"""Localized display titles for cards — bulk cache read + self-triggering background fill.

Persisted ``Title`` text is canonical (language-neutral) so the shared embedding space and the
English genre vocabulary stay clean (docs/data-model.md, "Canonical vs localized text") — which
means a French user would otherwise see "Kara Sevda" on every card where TMDB-fr says
"Amour éternel". The per-language display text lives in the ``title_localization`` cache; this
module makes it usable *at card scale*:

- :func:`display_titles` — the hot-path read: one bulk query for all of a response's title ids,
  never a TMDB fetch (the rows/search/chat latency budget is sacred — see the chat.turn.timing
  work). Titles without a cached localization simply show canonical this request.
- The misses are queued for a **background fill**: a daemon thread (mirroring
  :mod:`phare.embeddings.backfill` — in-process lock, single worker, tests drive it synchronously)
  fetches the localized TMDB detail for served-but-unlocalized titles, rate-limited to the same
  bulk cap as the metadata walker. Titles localize as they get *served*, so the cache converges on
  exactly what users look at — self-triggering, no CLI (principle 8), a strict no-op offline.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from phare.catalog.heal import RateLimiter, fetch_metadata_parallel
from phare.core.config import Settings, get_settings
from phare.core.i18n import DEFAULT_LANGUAGE, Language
from phare.db.models import Title, TitleLocalization
from phare.providers.types import MetadataProvider

logger = logging.getLogger(__name__)

# Same client-side cap as the bulk metadata walker (catalog/runtime_backfill): TMDB's guidance is
# ~50 rps and the 8-worker fan-out overshoots it unthrottled; wall-clock is irrelevant here.
_BULK_LOCALIZE_RPS = 30.0
# Titles fetched per drain batch (one commit each) — bounds memory and commit size.
_FILL_BATCH = 64
# Upper bound on queued ids per language, so a pathological burst of unlocalized rows can't grow
# the in-process queue without limit; anything dropped is simply re-queued next time it's served.
_MAX_PENDING = 1024

_lock = threading.Lock()
_pending: dict[Language, set[uuid.UUID]] = {}
_running = False


def display_titles(
    session: Session, language: Language, title_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Cached localized display names for ``title_ids``, in ONE query. Never fetches TMDB.

    Returns ``{}`` for the canonical language (English cards already show canonical text) and maps
    only the titles with a cached localized name — the caller leaves the rest canonical. Staleness
    is fine for a card (names don't churn; the detail open refreshes past the TTL), so the read
    ignores ``fetched_at``. A cached row *without* a name (pre-column cache fills) counts as
    missing, so old rows heal too. Every miss is queued for the background fill — the next serve
    of the same title finds it cached.
    """
    if language == DEFAULT_LANGUAGE or not title_ids:
        return {}
    rows = session.execute(
        select(TitleLocalization.title_id, TitleLocalization.title).where(
            TitleLocalization.language == language,
            TitleLocalization.title_id.in_(set(title_ids)),
        )
    ).all()
    found = {title_id: name for title_id, name in rows if name}
    missing = [title_id for title_id in set(title_ids) if title_id not in found]
    if missing:
        schedule_localization_fill(get_settings(), language, missing)
    return found


def upsert_localization(
    session: Session,
    title_id: uuid.UUID,
    language: Language,
    *,
    title: str | None,
    overview: str | None,
    genres: list[str],
    when: datetime,
) -> None:
    """Cache (or refresh) a title's localized display text. The caller owns the commit."""
    values = {"title": title, "overview": overview, "genres": genres, "fetched_at": when}
    session.execute(
        pg_insert(TitleLocalization)
        .values(title_id=title_id, language=language, **values)
        .on_conflict_do_update(index_elements=["title_id", "language"], set_=values)
    )


def run_localization_fill(
    session: Session,
    source: MetadataProvider,
    language: Language,
    title_ids: Iterable[uuid.UUID],
    *,
    limiter: RateLimiter | None = None,
) -> int:
    """Fetch + cache localized display text for the given titles. The caller owns the commit.

    ``source`` must be bound to ``language`` (its responses ARE the localized text — display only,
    it never touches the canonical ``Title`` row). Titles already localized (a cached row *with* a
    name) are skipped without a fetch, so concurrent detail-sheet fills and re-queues converge to
    no-ops. Best-effort per title (a flaky fetch is logged and skipped by the shared fetch
    primitive); returns how many localizations were written. Split out from the drain loop so
    tests drive it synchronously on a rolled-back session.
    """
    wanted = set(title_ids)
    if not wanted:
        return 0
    already = set(
        session.scalars(
            select(TitleLocalization.title_id).where(
                TitleLocalization.language == language,
                TitleLocalization.title_id.in_(wanted),
                TitleLocalization.title.is_not(None),
            )
        )
    )
    rows = session.scalars(
        select(Title).where(Title.id.in_(wanted - already), Title.tmdb_id.is_not(None))
    ).all()
    if not rows:
        return 0
    now = datetime.now(UTC)
    written = 0
    for title_id, meta in fetch_metadata_parallel(source, rows, limiter=limiter).items():
        upsert_localization(
            session,
            title_id,
            language,
            title=meta.title,
            overview=meta.overview,
            genres=list(meta.genres),
            when=now,
        )
        written += 1
    return written


def localization_fill_running() -> bool:
    """True while a background localization fill is in flight (single-process, in-memory state)."""
    with _lock:
        return _running


def schedule_localization_fill(
    settings: Settings,
    language: Language,
    title_ids: Sequence[uuid.UUID],
    *,
    runner: Callable[[Callable[[], None]], None] | None = None,
) -> bool:
    """Queue titles for localized-text caching and ensure one background worker is draining.

    Returns True if this call started the worker, False when one was already running (the new ids
    just join its queue), the queue is full, or there's nothing to do (offline / no ids — a strict
    no-op without TMDB, principle 5). ``runner`` executes the work and defaults to a daemon
    thread; tests pass their own to stay synchronous and off real threads.
    """
    global _running
    if not settings.tmdb_api_key or not title_ids:
        return False
    with _lock:
        queued = _pending.setdefault(language, set())
        room = _MAX_PENDING - len(queued)
        if room > 0:
            queued.update(list(title_ids)[:room])
        if _running or not queued:
            return False
        _running = True
    (runner or _spawn_daemon)(lambda: _drain(settings))
    return True


def _spawn_daemon(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="localization-fill", daemon=True).start()


def _pop_batch() -> tuple[Language, list[uuid.UUID]] | None:
    """Take the next batch off the queue, or atomically mark the worker stopped when empty.

    The emptiness check and the ``_running`` flip happen under one lock acquisition, so a request
    queueing ids concurrently either lands them before this drain sees "empty" or observes
    ``_running == False`` and starts a fresh worker — ids can't strand in the queue.
    """
    global _running
    with _lock:
        for language, queued in _pending.items():
            if queued:
                batch = [queued.pop() for _ in range(min(len(queued), _FILL_BATCH))]
                return language, batch
        _running = False
        return None


def _drain(settings: Settings) -> None:
    """Worker body: drain the queue batch-by-batch until :func:`_pop_batch` reports empty.

    On the happy path ``_pop_batch`` clears ``_running`` atomically with the emptiness check; on a
    crash the ``except`` clears it (and the queue — the un-drained ids simply re-queue the next
    time those titles are served, same self-healing as a dropped batch).
    """
    global _running
    from phare.db.base import get_session_factory
    from phare.providers.tmdb import TMDBMetadataProvider

    limiter = RateLimiter(_BULK_LOCALIZE_RPS)
    sources: dict[Language, MetadataProvider] = {}
    total = 0
    try:
        while (item := _pop_batch()) is not None:
            language, batch = item
            source = sources.setdefault(
                language,
                TMDBMetadataProvider(
                    api_key=settings.tmdb_api_key,
                    base_url=settings.tmdb_base_url,
                    language=language,
                    cache_ttl=settings.tmdb_cache_ttl_seconds,
                ),
            )
            with get_session_factory()() as session:
                total += run_localization_fill(session, source, language, batch, limiter=limiter)
                session.commit()
        logger.info("catalog.localization_fill.done", extra={"localized_count": total})
    except Exception:  # noqa: BLE001 - a background fill must never crash the process
        logger.exception("catalog.localization_fill.failed")
        with _lock:
            _pending.clear()
            _running = False
