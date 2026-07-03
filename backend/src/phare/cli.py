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


@app.command(name="import-catalog")
def import_catalog(
    scope: Annotated[
        str, typer.Option(help="'popular' (light page top-up) or 'broad' (deep genre sweep)")
    ] = "popular",
    pages: Annotated[int, typer.Option(help="popular: pages per kind")] = 1,
    pages_per_genre: Annotated[int, typer.Option(help="broad: discover pages per genre")] = 20,
    min_vote_count: Annotated[int, typer.Option(help="broad: TMDB vote-count floor")] = 50,
    embed: Annotated[bool, typer.Option(help="Embed missing titles after import")] = True,
    confirm: Annotated[bool, typer.Option(help="Allow running outside production")] = False,
) -> None:
    """Import the TMDB catalog into the candidate pool, then embed it.

    'broad' sweeps discover per genre (vote-count sorted) to seed the lesser-known long tail, far
    past the popular front page — this is the deep one-time seed and can run for minutes.

    Refuses to run unless ENVIRONMENT=production or --confirm is given, so a dev box can't fan out
    thousands of TMDB requests by accident.
    """
    from phare.catalog.service import broad_import_from_tmdb, import_from_tmdb
    from phare.db.base import get_session_factory
    from phare.embeddings.service import EmbeddingService
    from phare.embeddings.version import embedding_model_version, get_embedding_provider
    from phare.providers.tmdb import TMDBMetadataProvider

    settings = get_settings()
    configure_logging(settings.log_level)
    if scope not in ("popular", "broad"):
        typer.echo(f"Unknown scope {scope!r} (use 'popular' or 'broad').", err=True)
        raise typer.Exit(code=2)
    if not settings.is_production and not confirm:
        typer.echo("Refusing to import outside production without --confirm.", err=True)
        raise typer.Exit(code=2)
    if not settings.tmdb_api_key:
        typer.echo("TMDB_API_KEY must be set to import.", err=True)
        raise typer.Exit(code=2)

    provider = TMDBMetadataProvider(
        api_key=settings.tmdb_api_key,
        base_url=settings.tmdb_base_url,
        cache_ttl=settings.tmdb_cache_ttl_seconds,
    )
    with get_session_factory()() as session:
        if scope == "broad":
            created = broad_import_from_tmdb(
                session, provider, pages_per_genre=pages_per_genre, min_vote_count=min_vote_count
            )
        else:
            created = import_from_tmdb(session, provider, pages=pages)
        session.commit()
        typer.echo(f"Imported {created} new titles.")
        if embed:
            embedded = EmbeddingService(
                session, get_embedding_provider(settings), embedding_model_version(settings)
            ).embed_missing()
            session.commit()
            typer.echo(f"Embedded {embedded} titles.")


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
        # State up front which checks this run exercises, so a green run is never mistaken for
        # having covered more than it did (the similarity-spread check is embedder-dependent).
        from phare.eval.harness import alignment_checks_summary

        typer.echo(
            f"Alignment checks: {alignment_checks_summary(embedding_model_version(settings))}"
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
            for reason in result.alignment_failures:
                typer.echo(f"    alignment: {reason}")
            failures += 0 if result.passed else 1
    finally:
        transaction.rollback()
        connection.close()

    if failures:
        raise typer.Exit(code=1)


@app.command()
def conversion(
    profile: Annotated[
        str | None, typer.Option(help="Profile UUID; omit to aggregate across all profiles")
    ] = None,
    top_k: Annotated[int, typer.Option(help="Top-K slate size to score")] = 10,
    within_days: Annotated[int, typer.Option(help="Conversion window in days")] = 14,
) -> None:
    """Print the closed-loop conversion metric (top-K shown, watched within N days)."""
    import uuid as _uuid

    from sqlalchemy.orm import Session

    from phare.db.base import get_engine
    from phare.eval.conversion import conversion_stats, format_conversion

    configure_logging(get_settings().log_level)
    profile_id = _uuid.UUID(profile) if profile else None
    with Session(get_engine()) as session:
        stats = conversion_stats(
            session, profile_id=profile_id, top_k=top_k, within_days=within_days
        )
    scope = f"profile {profile}" if profile else "all profiles"
    for line in format_conversion(stats, scope):
        typer.echo(line)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
