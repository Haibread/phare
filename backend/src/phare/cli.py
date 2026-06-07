"""Phare command-line entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from phare.core.config import get_settings
from phare.core.logging import configure_logging

app = typer.Typer(help="Phare backend.", no_args_is_help=True)

# Project root that holds alembic.ini / migrations (src/phare/cli.py -> backend/).
_ROOT = Path(__file__).resolve().parents[2]


@app.command()
def serve(
    host: Annotated[str, typer.Option(envvar="HOST", help="Bind host")] = "0.0.0.0",
    port: Annotated[int, typer.Option(envvar="PORT", help="Bind port")] = 8000,
) -> None:
    """Run the API server (graceful shutdown on SIGTERM/SIGINT via uvicorn)."""
    import uvicorn

    uvicorn.run("phare.api.app:create_app", factory=True, host=host, port=port)


@app.command()
def migrate() -> None:
    """Apply database migrations up to head."""
    from alembic import command
    from alembic.config import Config

    configure_logging(get_settings().log_level)
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, "head")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
