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
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.catalog.heal import apply_metadata_heal, fetch_metadata_parallel
from phare.core.config import Settings
from phare.db.models import Title
from phare.providers.types import MetadataProvider

logger = logging.getLogger(__name__)

# Above this share of runtime-less titles the runtime cap operates on a ghost catalog, so the boot
# path schedules a bulk heal. Mirrors ``_MAX_RATING_GAP`` — a gap gauge that fires once after a
# broad seed, then never again as coverage stays healthy.
_MAX_RUNTIME_GAP = 0.5
# Rows healed per batch (one commit each) so a large catalog makes steady, observable progress and a
# crash loses at most one batch. Each row is one TMDB detail call, fanned out at the shared worker
# concurrency; the batch bounds memory + commit size, not the fan-out width.
_RUNTIME_BATCH = 64
# Safety bound on a single boot's heal so a pathological catalog can't spin the thread forever; the
# next boot resumes where this left off (idempotent — it only ever re-queries NULL runtimes).
_MAX_RUNTIME_BATCHES = 200

_lock = threading.Lock()
_running = False


def runtime_gap(session: Session) -> float:
    """Share of titles with no ``runtime_minutes`` — the data a runtime cap needs to filter on."""
    total = session.scalar(select(func.count()).select_from(Title)) or 0
    if total == 0:
        return 0.0
    missing = (
        session.scalar(
            select(func.count()).select_from(Title).where(Title.runtime_minutes.is_(None))
        )
        or 0
    )
    return missing / total


def runtime_backfill_running() -> bool:
    """True while a background runtime backfill is in flight (single-process, in-memory state)."""
    with _lock:
        return _running


def run_runtime_backfill(session: Session, source: MetadataProvider) -> int:
    """Fill missing runtimes (+ any missing quality signal) for NULL-runtime titles with a tmdb_id.

    Walks the backlog in batches, committing each so progress survives a crash and is observable in
    the logs. Idempotent: it re-queries the NULL-runtime rows each batch, so a title whose runtime
    got filled drops out; a title TMDB can't fill for stays NULL and is skipped next time it's
    picked up (bounded by ``_MAX_RUNTIME_BATCHES``, since it would otherwise be re-selected
    forever). Split out from the thread wrapper so tests can drive it synchronously; the caller owns
    the final state, this commits its own batches. Returns the count of runtimes filled.
    """
    filled = 0
    for _ in range(_MAX_RUNTIME_BATCHES):
        rows = (
            session.execute(
                select(Title)
                .where(Title.runtime_minutes.is_(None), Title.tmdb_id.is_not(None))
                .order_by(Title.tmdb_id, Title.id)
                .limit(_RUNTIME_BATCH)
            )
            .scalars()
            .all()
        )
        if not rows:
            break
        by_id = {row.id: row for row in rows}
        batch_filled = 0
        for title_id, meta in fetch_metadata_parallel(source, rows).items():
            if apply_metadata_heal(by_id[title_id], meta):
                batch_filled += 1
        session.commit()
        filled += batch_filled
        logger.info(
            "catalog.runtime_backfill.progress",
            extra={"batch_filled": batch_filled, "total_filled": filled},
        )
        # A batch that filled nothing is titles TMDB won't return a runtime for; they'd be re-picked
        # every batch, so stop rather than loop the same unfillable rows to the safety bound.
        if batch_filled == 0:
            break
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
            filled = run_runtime_backfill(session, source)
        logger.info("catalog.runtime_backfill.done", extra={"filled_count": filled})
    except Exception:  # noqa: BLE001 - a backfill must never crash the process
        logger.exception("catalog.runtime_backfill.failed")
    finally:
        with _lock:
            _running = False
