"""On-startup candidate-pool seeding.

A fresh self-hosted instance has no catalog to recommend *from*: the ``title`` table is populated
only by imported watch history, and every one of those is excluded as "already watched". So
recommendations (and the chat agent's picks) come back empty until someone runs a catalog import.

This closes that gap: when the pool is thin and TMDB is configured, pull TMDB's popular titles into
the candidate pool and embed them. Idempotent (upsert by ``tmdb_id``) and best-effort — it runs in
a background thread off the request path and never crashes startup. For a deeper, long-tail seed an
operator still runs ``phare import-catalog --scope broad``.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.core.config import Settings
from phare.db.models import Title, WatchEvent

logger = logging.getLogger(__name__)

# Below this many never-watched titles the pool is too thin to recommend from, so seed it.
_MIN_POOL = 50
# TMDB popular pages per kind (movies + shows) — a few hundred titles, enough to get going.
_SEED_PAGES = 3


def candidate_pool_size(session: Session) -> int:
    """Count titles no profile has watched — the pool recommendations actually draw from."""
    watched = select(WatchEvent.title_id)
    count = session.scalar(select(func.count()).select_from(Title).where(Title.id.notin_(watched)))
    return count or 0


def run_seed(
    session: Session, source: object, embed_provider: object, model_version: str
) -> tuple[int, int]:
    """Import popular titles from ``source`` + embed missing vectors, on an existing session.

    Returns ``(created, embedded)``. Split out from the wiring so it's testable with a fake source.
    """
    from phare.catalog.service import import_from_tmdb
    from phare.embeddings.service import EmbeddingService

    created = import_from_tmdb(session, source, pages=_SEED_PAGES)  # type: ignore[arg-type]
    session.commit()
    embedded = EmbeddingService(session, embed_provider, model_version).embed_missing()  # type: ignore[arg-type]
    session.commit()
    # NB: not "created"/"embedded" as keys — "created" is a reserved LogRecord attribute and
    # collides, raising inside the log call.
    logger.info(
        "catalog.autoseed.done", extra={"created_count": created, "embedded_count": embedded}
    )
    return created, embedded


def autoseed_scope(settings: Settings) -> str:
    """Resolve the autoseed scope. ``auto`` means broad in production (so a prod box deep-seeds
    itself, no operator command) and popular elsewhere (so dev never fans out tens of thousands)."""
    scope = settings.catalog_autoseed_scope
    if scope == "auto":
        return "broad" if settings.is_production else "popular"
    return scope


def seed_catalog_if_empty(settings: Settings) -> int:
    """Seed the catalog + embed it when the candidate pool is thin. Returns titles created (0 when
    skipped). Scope (popular vs the deep broad sweep) follows ``autoseed_scope``. Best-effort: logs
    and swallows everything so it can't take down startup."""
    if not settings.tmdb_api_key:
        logger.info("catalog.autoseed.skipped", extra={"reason": "no TMDB_API_KEY"})
        return 0
    # Imports are local so a no-TMDB / disabled deployment never pays for them.
    from phare.catalog.service import broad_import_from_tmdb
    from phare.db.base import get_session_factory
    from phare.embeddings.service import EmbeddingService
    from phare.embeddings.version import embedding_model_version, get_embedding_provider
    from phare.providers.tmdb import TMDBMetadataProvider

    scope = autoseed_scope(settings)
    try:
        with get_session_factory()() as session:
            pool = candidate_pool_size(session)
            if pool >= _MIN_POOL:
                logger.info("catalog.autoseed.skipped", extra={"reason": "pool ok", "pool": pool})
                return 0
            logger.info("catalog.autoseed.start", extra={"pool": pool, "scope": scope})
            provider = TMDBMetadataProvider(
                api_key=settings.tmdb_api_key,
                base_url=settings.tmdb_base_url,
                cache_ttl=settings.tmdb_cache_ttl_seconds,
            )
            if scope == "broad":
                created = broad_import_from_tmdb(
                    session,
                    provider,
                    pages_per_genre=settings.catalog_broad_pages_per_genre,
                    min_vote_count=settings.catalog_broad_min_vote_count,
                )
                session.commit()
                EmbeddingService(
                    session, get_embedding_provider(settings), embedding_model_version(settings)
                ).embed_missing()
                session.commit()
                return created
            created, _ = run_seed(
                session,
                provider,
                get_embedding_provider(settings),
                embedding_model_version(settings),
            )
            return created
    except Exception:  # noqa: BLE001 - seeding must never crash startup
        logger.exception("catalog.autoseed.failed")
        return 0
