"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from phare.api import (
    actions,
    auth,
    catalog,
    chat,
    feedback,
    health,
    history,
    memory,
    profiles,
    recommend,
    sync,
    taste,
    titles,
)
from phare.core.auth import get_current_user
from phare.core.config import get_settings
from phare.core.logging import configure_logging
from phare.core.ratelimit import RateLimitMiddleware
from phare.core.telemetry import setup_telemetry
from phare.db.base import get_engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.migrate_on_startup:
        from phare.db.migrate import run_migrations

        run_migrations()
    logger.info("startup")
    # Seed the candidate pool in the background if it's empty — a fresh instance otherwise has
    # nothing to recommend from. Off the request path (a network import), so readiness isn't gated
    # on it; skipped entirely without TMDB or when disabled.
    if settings.catalog_autoseed and settings.tmdb_api_key:
        import threading

        from phare.catalog.bootstrap import seed_catalog_if_empty

        threading.Thread(
            target=seed_catalog_if_empty,
            args=(settings,),
            name="catalog-autoseed",
            daemon=True,
        ).start()
    # Ongoing freshness: a recurring background pull of new/current releases (no-op when disabled or
    # without TMDB). Held so it can be stopped cleanly on shutdown.
    from phare.catalog.refresh import start_refresh_loop

    stop_refresh = start_refresh_loop(settings)
    yield
    stop_refresh()
    logger.info("shutdown")


def create_app() -> FastAPI:
    """Build and configure the FastAPI app."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="Phare", version="0.1.0", lifespan=_lifespan)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Rate limit the expensive/abusable endpoints (login brute-force, agent-model chat, bulk
    # imports) before they reach a handler (review I1). Added last so it runs first (outermost).
    app.add_middleware(RateLimitMiddleware, settings=settings)

    # Open endpoints: health + auth must be reachable without a token.
    app.include_router(health.router)
    app.include_router(auth.router)

    # Data endpoints: gated by get_current_user — every request needs a valid token (closed by
    # default). Endpoints that need the identity re-declare the dependency to read it.
    guarded = [Depends(get_current_user)]
    app.include_router(profiles.router, dependencies=guarded)
    app.include_router(history.router, dependencies=guarded)
    app.include_router(sync.router, dependencies=guarded)
    app.include_router(taste.router, dependencies=guarded)
    app.include_router(catalog.router, dependencies=guarded)
    app.include_router(recommend.router, dependencies=guarded)
    app.include_router(titles.router, dependencies=guarded)
    app.include_router(chat.router, dependencies=guarded)
    app.include_router(feedback.router, dependencies=guarded)
    app.include_router(actions.router, dependencies=guarded)
    app.include_router(memory.router, dependencies=guarded)

    setup_telemetry(app, get_engine(), settings)
    logger.info("app.ready", extra={"environment": settings.environment})
    return app
