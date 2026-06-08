"""Phare command-line entrypoint."""

from __future__ import annotations

from typing import Annotated

import typer

from phare.core.config import get_settings
from phare.core.logging import configure_logging
from phare.db.migrate import run_migrations

app = typer.Typer(help="Phare backend.", no_args_is_help=True)


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
    configure_logging(get_settings().log_level)
    run_migrations()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
