"""Background bulk runtime backfill — heals the catalog's missing runtimes off the boot path.

The broad TMDB *discover* import omits runtime, so a freshly-seeded catalog has ``runtime_minutes``
NULL on almost every title (measured live: 3.2% coverage). The read-path heals only tiny slices —
a detail sheet opening, or up to 64 chat-pool candidates on a runtime-capped turn — so a "something
under 90 minutes" request (and the SQL-side runtime filter in round-7 finding 1) operates on a ghost
catalog. This module fills the gap in bulk: at boot, when coverage is poor, a single background
daemon thread walks the NULL-runtime rows, fetches each title's TMDB detail with bounded concurrency
(the shared primitive in :mod:`phare.catalog.heal`), and persists runtime + any missing quality
signal. Idempotent (only NULLs, never clobbers), best-effort, and a strict no-op without TMDB.

Mirrors :mod:`phare.embeddings.backfill`: an in-process lock guarantees one backfill at a time (the
app is single-process), and the work is split out from the thread wrapper so tests can drive it
synchronously on a rolled-back session instead of spawning a real thread with its own DB connection.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.orm import Session

from phare.catalog.heal import RateLimiter, apply_metadata_heal, fetch_metadata_parallel
from phare.core.config import Settings
from phare.db.models import Title
from phare.providers.types import MetadataProvider

logger = logging.getLogger(__name__)

# Client-side request cap for the *bulk* walker against TMDB. Their guidance is ~50 rps; the
# 8-worker fan-out measured ~176 rps live on a warm CDN — enough to trip a 429 storm the keyset
# cursor would then permanently skip whole batches over until the next boot. 30 rps keeps a
# comfortable margin; wall-clock is irrelevant for a background job. The read path is never capped.
_BULK_HEAL_RPS = 30.0


def _metadata_gap_predicate() -> Any:
    """A title has a fillable metadata gap when it's missing a runtime *or* an original language.

    A single detail fetch carries runtime, votes, credits *and* language, so this one predicate
    walks every row a heal could still improve — crucially, one whose runtime was already healed
    live but that never got credits/language (a runtime-only predicate would skip all of those).
    Language is the honest sentinel for "never deep-fetched": credits arrive in the same call, so a
    row with a language also has whatever credits TMDB had. ``vote`` gaps aren't included — they're
    a bonus of the same fetch, not a reason to walk the catalog again."""
    return or_(Title.runtime_minutes.is_(None), Title.original_language.is_(None))


# Above this share of gapped titles the catalog is missing enough metadata (runtime/credits/
# language) that the boot path schedules a bulk heal. Mirrors ``_MAX_RATING_GAP`` — a gap gauge that
# fires once after a broad seed, then never again as coverage stays healthy.
_MAX_METADATA_GAP = 0.5
# Rows healed per batch (one commit each) so a large catalog makes steady, observable progress and a
# crash loses at most one batch. Each row is one TMDB detail call, fanned out at the shared worker
# concurrency; the batch bounds memory + commit size, not the fan-out width.
_RUNTIME_BATCH = 64
# Safety bound on a single boot's heal so a pathological catalog can't spin the thread forever; the
# next boot resumes where this left off (idempotent — it only ever re-queries gapped rows).
_MAX_RUNTIME_BATCHES = 200

_lock = threading.Lock()
_running = False


def metadata_gap(session: Session) -> float:
    """Share of titles with a fillable metadata gap (:func:`_metadata_gap_predicate`).

    Drives the boot gate: a catalog freshly seeded from *discover* has almost every row missing
    runtime and/or original language, so the gauge is high and one background pass is scheduled;
    once the walker has deep-fetched the catalog it drops to ~0 and never fires again. A catalog
    that got its runtimes healed live but has 0% credits/language still reads a large gap (language
    is NULL), so it still triggers a pass — the round-8 requirement."""
    total = session.scalar(select(func.count()).select_from(Title)) or 0
    if total == 0:
        return 0.0
    missing = (
        session.scalar(select(func.count()).select_from(Title).where(_metadata_gap_predicate()))
        or 0
    )
    return missing / total


def runtime_backfill_running() -> bool:
    """True while a background runtime backfill is in flight (single-process, in-memory state)."""
    with _lock:
        return _running


def run_metadata_backfill(
    session: Session, source: MetadataProvider, *, limiter: RateLimiter | None = None
) -> int:
    """Fill missing metadata (runtime, votes, credits, original language) for gapped titles.

    Selects rows with a fillable gap (:func:`_metadata_gap_predicate` — missing runtime *or*
    language; one detail fetch carries both plus credits + votes) and a ``tmdb_id``. Walks the
    backlog in batches behind a keyset cursor on ``(tmdb_id, id)``, committing each batch so
    progress survives a crash and is observable in the logs. The cursor is what makes one pass
    terminate: a title TMDB can't improve (no runtime *and* no language for it — a stale id) is
    *passed over*, never re-selected — measured live, an early all-unfillable batch otherwise
    stalled the heal at 609/7400. Idempotent across boots: healed rows drop out of the gap
    predicate. Split out from the thread wrapper so tests can drive it synchronously; the caller
    owns the final state, this commits its own batches. Returns the count of titles healed.

    Every fetch passes through a :class:`RateLimiter` (default ``_BULK_HEAL_RPS``) so this bulk
    walk stays well under TMDB's rps guidance — a 429 storm here would make the keyset cursor skip
    whole batches permanently until the next boot. Tests inject their own limiter or ``None``.
    """
    limiter = limiter if limiter is not None else RateLimiter(_BULK_HEAL_RPS)
    filled = 0
    cursor: tuple[int, uuid.UUID] | None = None
    for _ in range(_MAX_RUNTIME_BATCHES):
        query = (
            select(Title)
            .where(_metadata_gap_predicate(), Title.tmdb_id.is_not(None))
            .order_by(Title.tmdb_id, Title.id)
            .limit(_RUNTIME_BATCH)
        )
        if cursor is not None:
            query = query.where(tuple_(Title.tmdb_id, Title.id) > cursor)
        rows = session.execute(query).scalars().all()
        if not rows:
            break
        cursor = (rows[-1].tmdb_id, rows[-1].id)  # type: ignore[assignment]  # tmdb_id filtered non-NULL
        by_id = {row.id: row for row in rows}
        batch_filled = 0
        for title_id, meta in fetch_metadata_parallel(source, rows, limiter=limiter).items():
            if apply_metadata_heal(by_id[title_id], meta):
                batch_filled += 1
        session.commit()
        filled += batch_filled
        logger.info(
            "catalog.metadata_backfill.progress",
            extra={"batch_filled": batch_filled, "total_filled": filled},
        )
    return filled


def schedule_runtime_backfill(
    settings: Settings,
    *,
    runner: Callable[[Callable[[], None]], None] | None = None,
) -> bool:
    """Start a single background runtime backfill when the catalog's runtime gap is large.

    Returns True if this call started one, False if one was already running or there's nothing to do
    (no TMDB key, or coverage already healthy). No-ops cleanly offline (principle 5) and needs no
    CLI command (principle 8) — it self-triggers at boot; the read path keeps healing as chat runs.
    ``runner`` executes the work and defaults to a daemon thread; tests pass their own to stay
    synchronous and off real threads (which open their own uncontrolled DB connections).
    """
    global _running
    if not settings.tmdb_api_key:
        return False  # offline — nothing to fetch runtimes from
    with _lock:
        if _running:
            return False
        _running = True
    (runner or _spawn_daemon)(lambda: _run_backfill(settings))
    return True


def _spawn_daemon(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="runtime-backfill", daemon=True).start()


def _run_backfill(settings: Settings) -> None:
    global _running
    try:
        from phare.db.base import get_session_factory
        from phare.providers.tmdb import TMDBMetadataProvider

        source = TMDBMetadataProvider(
            api_key=settings.tmdb_api_key,
            base_url=settings.tmdb_base_url,
            cache_ttl=settings.tmdb_cache_ttl_seconds,
        )
        with get_session_factory()() as session:
            filled = run_metadata_backfill(session, source)
        logger.info("catalog.metadata_backfill.done", extra={"filled_count": filled})
    except Exception:  # noqa: BLE001 - a backfill must never crash the process
        logger.exception("catalog.metadata_backfill.failed")
    finally:
        with _lock:
            _running = False
