"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from phare.api import health, history
from phare.core.config import get_settings
from phare.core.logging import configure_logging
from phare.core.telemetry import setup_telemetry
from phare.db.base import get_engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("startup")
    yield
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

    app.include_router(health.router)
    app.include_router(history.router)

    setup_telemetry(app, get_engine(), settings)
    logger.info("app.ready", extra={"environment": settings.environment})
    return app
