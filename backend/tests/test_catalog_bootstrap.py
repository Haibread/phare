"""Auto-seed bootstrap: the candidate-pool gauge and the no-TMDB skip (kept hermetic — the seed
itself hits TMDB, so we only exercise the guards here, not a live import)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.catalog.bootstrap import candidate_pool_size, run_seed, seed_catalog_if_empty
from phare.catalog.sample import seed_sample_catalog
from phare.core.config import get_settings
from phare.db.models import EventType, Profile, Title, TitleKind, WatchEvent
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.types import TitleMetadata


class _FakePopularSource:
    """A catalog source that returns a fixed popular page for movies (and nothing else)."""

    def __init__(self, metas: list[TitleMetadata]) -> None:
        self._metas = metas

    def popular(self, kind: TitleKind, page: int = 1) -> list[TitleMetadata]:
        return self._metas if (kind is TitleKind.movie and page == 1) else []


def test_candidate_pool_size_counts_only_unwatched(db_session: Session) -> None:
    assert candidate_pool_size(db_session) == 0  # empty DB → nothing to recommend

    seed_sample_catalog(db_session)
    db_session.flush()
    base = candidate_pool_size(db_session)
    assert base > 0  # catalog titles, none watched yet

    # Watching a catalog title removes exactly it from the pool.
    title = db_session.scalars(select(Title)).first()
    assert title is not None
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()
    db_session.add(
        WatchEvent(profile_id=profile.id, title_id=title.id, type=EventType.watched, source="test")
    )
    db_session.flush()
    assert candidate_pool_size(db_session) == base - 1


def test_autoseed_scope_auto_resolves_by_environment() -> None:
    from phare.catalog.bootstrap import autoseed_scope
    from phare.core.config import Settings

    # "auto" deep-seeds production (broad) but keeps dev light (popular)...
    assert (
        autoseed_scope(Settings(environment="production", catalog_autoseed_scope="auto")) == "broad"
    )
    assert (
        autoseed_scope(Settings(environment="development", catalog_autoseed_scope="auto"))
        == "popular"
    )
    # ...and an explicit scope is always honoured verbatim.
    assert (
        autoseed_scope(Settings(environment="production", catalog_autoseed_scope="popular"))
        == "popular"
    )
    assert (
        autoseed_scope(Settings(environment="development", catalog_autoseed_scope="broad"))
        == "broad"
    )


def test_invalid_autoseed_scope_fails_loud_at_startup() -> None:
    # A typo'd scope must raise at config load, not silently fall back to popular.
    import pytest
    from pydantic import ValidationError

    from phare.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(catalog_autoseed_scope="brod")


def test_seed_is_skipped_without_a_tmdb_key() -> None:
    # conftest blanks TMDB_API_KEY, so the configured settings have no key: the seed must no-op
    # (and never touch the network) rather than raise.
    assert seed_catalog_if_empty(get_settings()) == 0


def test_run_seed_imports_embeds_and_logs_without_raising(db_session: Session) -> None:
    # Exercises the full success path — including the "done" log line, which once crashed because
    # "created" is a reserved LogRecord attribute. A fake source keeps it off the network.
    metas = [
        TitleMetadata(
            kind=TitleKind.movie,
            tmdb_id=9000 + i,
            title=f"Seed {i}",
            year=2020,
            overview="A freshly seeded candidate.",
        )
        for i in range(3)
    ]
    created, embedded = run_seed(
        db_session, _FakePopularSource(metas), LocalHashEmbeddingProvider(), LOCAL_MODEL_VERSION
    )
    assert created == 3
    assert embedded >= 3
    assert candidate_pool_size(db_session) == 3  # all seeded titles are recommendable
