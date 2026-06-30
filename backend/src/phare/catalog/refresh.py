"""Ongoing catalog freshness.

A seeded catalog goes stale: new movies and shows keep coming out, and nothing on the *read* path
can surface a title that isn't imported yet. So freshness needs a recurring pull — this module runs
one in a background daemon thread (no external cron, no scheduler dependency; principle 8), pulling
TMDB's current/new releases on an interval and embedding them.

The pull source is deliberately the *curated current-content* endpoints (this week's trending +
what's playing now / on the air), not vote-filtered discover — brand-new releases have too few votes
to clear a quality floor, and those are exactly what we want to catch. See ``refresh_from_tmdb``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from sqlalchemy.orm import Session

from phare.catalog.service import CatalogRefreshSource, refresh_from_tmdb
from phare.core.config import Settings

logger = logging.getLogger(__name__)

# How long a graceful shutdown waits for an in-flight refresh before giving up (the thread is a
# daemon, so it dies with the process regardless — this just lets a quick pass finish cleanly).
_SHUTDOWN_JOIN_TIMEOUT = 5.0


def run_refresh(
    session: Session,
    source: CatalogRefreshSource,
    embed_provider: object,
    model_version: str,
    *,
    pages: int = 1,
) -> tuple[int, int]:
    """Import current/new releases + embed the missing ones, on an existing session. Returns
    ``(created, embedded)``. Split out from the wiring so it's testable with a fake source."""
    from phare.embeddings.service import EmbeddingService

    created = refresh_from_tmdb(session, source, pages=pages)
    session.commit()
    embedded = EmbeddingService(session, embed_provider, model_version).embed_missing()  # type: ignore[arg-type]
    session.commit()
    logger.info(
        "catalog.refresh.done", extra={"created_count": created, "embedded_count": embedded}
    )
    return created, embedded


def run_refresh_once(settings: Settings) -> int:
    """One freshness pass against the live providers, on its own session. Returns titles created (0
    when skipped). Best-effort: logs and swallows everything so a flaky network never kills the loop
    (or, on the first tick, the process)."""
    if not settings.tmdb_api_key:
        return 0
    from phare.db.base import get_session_factory
    from phare.embeddings.version import embedding_model_version, get_embedding_provider
    from phare.providers.tmdb import TMDBMetadataProvider

    try:
        with get_session_factory()() as session:
            provider = TMDBMetadataProvider(
                api_key=settings.tmdb_api_key,
                base_url=settings.tmdb_base_url,
                cache_ttl=settings.tmdb_cache_ttl_seconds,
            )
            created, _ = run_refresh(
                session,
                provider,
                get_embedding_provider(settings),
                embedding_model_version(settings),
                pages=settings.catalog_refresh_pages,
            )
            return created
    except Exception:  # noqa: BLE001 - a refresh must never crash the loop / process
        logger.exception("catalog.refresh.failed")
        return 0


def start_refresh_loop(settings: Settings) -> Callable[[], None]:
    """Start the recurring freshness refresh in a daemon thread; return a ``stop()`` that ends it.

    No-op (returns a no-op stop) when disabled (``catalog_refresh_interval_seconds <= 0``) or with
    no TMDB key. The first run waits one full interval — startup already seeds, so there's no point
    pulling again immediately. ``stop()`` signals the loop and waits briefly for an in-flight pass.
    """
    interval = settings.catalog_refresh_interval_seconds
    if interval <= 0 or not settings.tmdb_api_key:
        return lambda: None

    stop = threading.Event()

    def loop() -> None:
        # Event.wait returns True only when stop is set; on timeout it returns False → run a pass.
        while not stop.wait(interval):
            run_refresh_once(settings)

    thread = threading.Thread(target=loop, name="catalog-refresh", daemon=True)
    thread.start()
    logger.info("catalog.refresh.scheduled", extra={"interval_seconds": interval})

    def shutdown() -> None:
        stop.set()
        thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT)

    return shutdown
