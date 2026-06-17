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


@app.command()
def evaluate(k: Annotated[int, typer.Option(help="Top-K slate size to score")] = 20) -> None:
    """Run the persona guardrail suite + anti-degeneracy metrics. Exits non-zero on a violation.

    Runs inside a rolled-back transaction, so it never writes evaluation data to your database.
    """
    from sqlalchemy.orm import Session

    from phare.db.base import get_engine
    from phare.embeddings.version import embedding_model_version, get_embedding_provider
    from phare.eval.harness import evaluate_all

    settings = get_settings()
    configure_logging(settings.log_level)

    connection = get_engine().connect()
    transaction = connection.begin()
    failures = 0
    try:
        session = Session(bind=connection)
        results = evaluate_all(
            session,
            embed_provider=get_embedding_provider(settings),
            model_version=embedding_model_version(settings),
            k=k,
        )
        for result in results:
            status = "PASS" if result.passed else "FAIL"
            typer.echo(
                f"[{status}] {result.name}: n={result.count} "
                f"pop_bias={result.popularity_bias:.2f} "
                f"diversity={result.intra_list_diversity:.2f} novelty={result.novelty:.2f}"
            )
            for title in result.forbidden_violations:
                typer.echo(f"    forbidden-genre leak: {title}")
            for title in result.recommended_watched:
                typer.echo(f"    recommended an already-watched title: {title}")
            failures += 0 if result.passed else 1
    finally:
        transaction.rollback()
        connection.close()

    if failures:
        raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
