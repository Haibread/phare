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


def backfill_missing_on_full_pool(
    session: Session, embed_provider: object, model_version: str
) -> int:
    """Embed titles still missing a vector for the active model version, on an existing session.

    Runs when autoseed *skips* (the pool is already full), so a catalog that has titles but not
    their vectors heals at boot instead of waiting for a read request or the daily refresh. Covers
    two cases a full pool otherwise hides: an embed pass interrupted mid-backlog (e.g. a restart
    during the initial seed), and an embedding-model change (every title "missing" the new version).
    Returns the count embedded. No-ops cheaply when nothing is missing. Best-effort commit is the
    caller's; embedding here mirrors the inline embed of the seed paths (dedup on write handles a
    concurrent read-path backfill).
    """
    from phare.embeddings.service import EmbeddingService

    svc = EmbeddingService(session, embed_provider, model_version)  # type: ignore[arg-type]
    if not svc.has_missing():
        return 0
    logger.info("catalog.autoseed.backfill_start")
    embedded = svc.embed_missing()
    logger.info("catalog.autoseed.backfill_done", extra={"embedded_count": embedded})
    return embedded


# Above this share of rating-less titles the quality floor is inert for most of the pool, so the
# boot path re-pulls the import as a metadata refresh. Discover pages carry ~20 titles per request,
# so even a broad refresh is a few hundred requests — not a per-title fan-out.
_MAX_RATING_GAP = 0.5


def rating_gap(session: Session) -> float:
    """Share of titles with no ``vote_average`` — the quality signal the re-ranker's floor needs."""
    total = session.scalar(select(func.count()).select_from(Title)) or 0
    if total == 0:
        return 0.0
    missing = (
        session.scalar(select(func.count()).select_from(Title).where(Title.vote_average.is_(None)))
        or 0
    )
    return missing / total


def heal_missing_quality_signal(session: Session, settings: Settings, source: object) -> int:
    """Re-run the (idempotent) catalog import as a metadata refresh when most titles lack a rating.

    An import bug used to drop ``vote_average`` on insert, leaving the re-ranker's quality floor
    with nothing to bite on for almost the whole pool — badly-rated titles rode pure similarity.
    ``upsert_titles`` refreshes ratings on existing rows, so one re-pull of the same discover pages
    heals the catalog in bulk at boot (principle 8: self-triggering, no operator command). No-ops
    once coverage is healthy, so this fires once after the fix ships and then never again. Returns
    the number of *new* titles the refresh happened to create (usually 0).
    """
    from phare.catalog.service import broad_import_from_tmdb, import_from_tmdb

    gap = rating_gap(session)
    if gap <= _MAX_RATING_GAP:
        return 0
    logger.info("catalog.autoseed.rating_heal_start", extra={"rating_gap": round(gap, 2)})
    if autoseed_scope(settings) == "broad":
        created = broad_import_from_tmdb(
            session,
            source,  # type: ignore[arg-type]
            pages_per_genre=settings.catalog_broad_pages_per_genre,
            min_vote_count=settings.catalog_broad_min_vote_count,
        )
    else:
        created = import_from_tmdb(session, source, pages=_SEED_PAGES)  # type: ignore[arg-type]
    session.commit()
    logger.info(
        "catalog.autoseed.rating_heal_done",
        extra={"created_count": created, "rating_gap_after": round(rating_gap(session), 2)},
    )
    return created


def _schedule_runtime_heal_if_gapped(session: Session, settings: Settings) -> bool:
    """Kick off the background bulk runtime backfill when the catalog's runtime gap is large.

    Split from the boot path so it's testable in isolation. The gauge + scheduler live in
    :mod:`phare.catalog.runtime_backfill`; the scheduler itself re-checks the TMDB key and dedups
    concurrent starts, so this only gates on the gap (cheap count) to avoid spinning up a thread
    when there's nothing to heal. Returns True when a heal was scheduled."""
    from phare.catalog.runtime_backfill import (
        _MAX_METADATA_GAP,
        metadata_gap,
        schedule_runtime_backfill,
    )

    gap = metadata_gap(session)
    if gap <= _MAX_METADATA_GAP:
        return False
    logger.info("catalog.autoseed.metadata_heal_scheduled", extra={"metadata_gap": round(gap, 2)})
    return schedule_runtime_backfill(settings)


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
                # A full pool isn't a *complete* one. First heal a mostly rating-less catalog (a
                # past import bug dropped vote_average, leaving the quality floor inert), THEN the
                # embedding backlog — in that order, so any titles the refresh creates get their
                # vectors in the same boot. Both no-op cheaply when nothing is missing.
                heal_missing_quality_signal(
                    session,
                    settings,
                    TMDBMetadataProvider(
                        api_key=settings.tmdb_api_key,
                        base_url=settings.tmdb_base_url,
                        cache_ttl=settings.tmdb_cache_ttl_seconds,
                    ),
                )
                backfill_missing_on_full_pool(
                    session,
                    get_embedding_provider(settings),
                    embedding_model_version(settings),
                )
                session.commit()
                # Runtime is the one field the discover import (and so the rating re-pull above)
                # can't fill — it needs a per-title detail call. When most of the catalog is still
                # runtime-less, schedule a BACKGROUND bulk heal so a runtime cap stops filtering on
                # a ghost catalog (finding 2); it runs off the boot path, so readiness isn't gated
                # on it, and no-ops when coverage is already healthy or TMDB is unset.
                _schedule_runtime_heal_if_gapped(session, settings)
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
