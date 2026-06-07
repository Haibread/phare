"""Programmatic Alembic upgrade, shared by the CLI and (optionally) app startup."""

from __future__ import annotations

import logging
from pathlib import Path

from phare.core.config import get_settings

logger = logging.getLogger(__name__)

# backend/ root holding alembic.ini and migrations/ (src/phare/db/migrate.py -> backend).
_ROOT = Path(__file__).resolve().parents[3]


def run_migrations() -> None:
    """Upgrade the database to the latest revision."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    logger.info("migrations.upgrade")
    command.upgrade(cfg, "head")
