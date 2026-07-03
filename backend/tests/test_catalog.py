"""Catalog seeding (offline sample) + TMDB import upsert behaviour."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from phare.api.catalog import ensure_import_allowed
from phare.catalog.sample import seed_sample_catalog
from phare.catalog.service import (
    broad_import_from_tmdb,
    import_from_tmdb,
    search_titles,
    upsert_titles,
)
from phare.cli import app as cli_app
from phare.core.config import Settings, get_settings
from phare.db.models import Title, TitleKind
from phare.providers.types import TitleMetadata
from tests.conftest import authed_client, make_account


def _count_titles(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Title)) or 0


def test_sample_catalog_titles_carry_real_posters(db_session: Session) -> None:
    # The sample-data path is a new user's first impression; every sample title must ship a real
    # TMDB poster so it isn't a wall of text blocks (works offline — the image CDN needs no key).
    seed_sample_catalog(db_session)
    posters = db_session.scalars(select(Title.poster_path)).all()
    assert posters and all(p and p.startswith("/") and p.endswith(".jpg") for p in posters)


def test_seed_sample_catalog_is_idempotent(db_session: Session) -> None:
    created = seed_sample_catalog(db_session)
    assert created > 20  # a diverse pool, not a couple of titles
    total_after_first = _count_titles(db_session)

    created_again = seed_sample_catalog(db_session)
    assert created_again == 0  # nothing new on a re-seed
    assert _count_titles(db_session) == total_after_first


def test_sample_catalog_spans_movies_and_shows(db_session: Session) -> None:
    seed_sample_catalog(db_session)
    kinds = set(db_session.scalars(select(Title.kind)).all())
    assert kinds == {TitleKind.movie, TitleKind.show}


def test_upsert_refreshes_popularity_without_duplicating(db_session: Session) -> None:
    meta = TitleMetadata(kind=TitleKind.movie, tmdb_id=999001, title="X", popularity=1.0)
    assert upsert_titles(db_session, [meta]) == 1

    updated = meta.model_copy(update={"popularity": 9.0})
    assert upsert_titles(db_session, [updated]) == 0  # no new row
    row = db_session.scalar(select(Title).where(Title.tmdb_id == 999001))
    assert row is not None and row.popularity == 9.0


class _FakeCatalogSource:
    """Returns canned popular lists per kind (the ``CatalogSource`` protocol)."""

    def __init__(self) -> None:
        self.calls: list[tuple[TitleKind, int]] = []

    def popular(self, kind: TitleKind, page: int = 1) -> list[TitleMetadata]:
        self.calls.append((kind, page))
        base = 1000 if kind is TitleKind.movie else 2000
        return [
            TitleMetadata(
                kind=kind, tmdb_id=base + page, title=f"{kind.value}-{page}", popularity=float(page)
            )
        ]


def test_import_from_tmdb_pulls_each_kind_and_page(db_session: Session) -> None:
    source = _FakeCatalogSource()
    created = import_from_tmdb(db_session, source, pages=2)

    assert created == 4  # 2 kinds x 2 pages
    assert (TitleKind.movie, 1) in source.calls
    assert (TitleKind.show, 2) in source.calls


class _FakeDiscoverSource:
    """Genres + paged discover results (the ``CatalogDiscoverSource`` protocol)."""

    _GENRES = {
        TitleKind.movie: {28: "Action", 18: "Drama"},
        TitleKind.show: {18: "Drama"},
    }

    def __init__(self) -> None:
        self.calls: list[tuple[TitleKind, int | None, int]] = []

    def genres(self, kind: TitleKind) -> dict[int, str]:
        return dict(self._GENRES[kind])

    def discover(
        self,
        kind: TitleKind,
        *,
        genre_id: int | None = None,
        min_vote_count: int = 0,
        page: int = 1,
    ) -> list[TitleMetadata]:
        self.calls.append((kind, genre_id, page))
        if page > 1:
            return []  # one page of results per genre, then dry
        if kind is TitleKind.movie:
            return [
                # Shared across every movie genre -> must dedupe to one row.
                TitleMetadata(kind=kind, tmdb_id=7000, title="Shared", overview="o"),
                TitleMetadata(kind=kind, tmdb_id=8000 + (genre_id or 0), title="M", overview="p"),
                # No overview -> dropped (empty embedding input).
                TitleMetadata(kind=kind, tmdb_id=9000 + (genre_id or 0), title="Empty"),
            ]
        return [TitleMetadata(kind=kind, tmdb_id=6000, title="Show", overview="s")]


def test_broad_import_dedupes_filters_and_stops_when_dry(db_session: Session) -> None:
    source = _FakeDiscoverSource()
    created = broad_import_from_tmdb(db_session, source)

    # movie: shared(7000) + M8028 + M8018 ; show: 6000 -> the no-overview rows are dropped.
    assert created == 4
    assert db_session.scalar(select(Title).where(Title.tmdb_id == 7000)) is not None
    assert db_session.scalar(select(Title).where(Title.tmdb_id == 9028)) is None
    # Walked to the first empty page per genre, not all 20.
    assert (TitleKind.movie, 28, 2) in source.calls
    assert (TitleKind.movie, 28, 3) not in source.calls


class _FakeSearchSource:
    """Returns canned search matches (the ``CatalogSearchSource`` protocol)."""

    def search(self, query: str, *, limit: int = 8) -> list[TitleMetadata]:
        return [
            TitleMetadata(
                kind=TitleKind.movie,
                tmdb_id=555001,
                title="Searched Movie",
                year=2020,
                popularity=5.0,
                poster_path="/s.jpg",
            )
        ]


def test_search_titles_finds_local_match(db_session: Session) -> None:
    seed_sample_catalog(db_session)
    target = db_session.scalars(select(Title)).first()
    assert target is not None
    results = search_titles(db_session, target.title)
    assert target.id in {t.id for t in results}


def test_search_titles_upserts_live_matches(db_session: Session) -> None:
    results = search_titles(db_session, "searched", _FakeSearchSource())
    assert any(t.tmdb_id == 555001 for t in results)
    # The live match was persisted so it becomes recommendable + requestable.
    assert db_session.scalar(select(Title).where(Title.tmdb_id == 555001)) is not None


def test_search_titles_empty_query_returns_nothing(db_session: Session) -> None:
    seed_sample_catalog(db_session)
    assert search_titles(db_session, "   ") == []


def test_search_titles_ranks_prefix_above_more_popular_substring(db_session: Session) -> None:
    # "tenet" should lead with *Tenet*, not with a more popular title that merely contains the
    # letters mid-word — word-start matches rank above mid-word substrings, ahead of popularity.
    prefix = Title(kind=TitleKind.movie, tmdb_id=770001, title="Tenet", popularity=1.0)
    midword = Title(kind=TitleKind.movie, tmdb_id=770002, title="Subtenet", popularity=99.0)
    db_session.add_all([prefix, midword])
    db_session.flush()

    results = search_titles(db_session, "tenet")
    ids = [t.id for t in results]
    assert ids.index(prefix.id) < ids.index(midword.id)


def test_search_titles_ranks_word_start_above_midword(db_session: Session) -> None:
    # A match at the start of an *inner* word ("... Story") beats a mid-word substring, even when
    # the substring match is far more popular.
    word_start = Title(kind=TitleKind.movie, tmdb_id=770011, title="A Toy Story", popularity=1.0)
    midword = Title(kind=TitleKind.movie, tmdb_id=770012, title="Prehistory", popularity=99.0)
    db_session.add_all([word_start, midword])
    db_session.flush()

    results = search_titles(db_session, "story")
    ids = [t.id for t in results]
    assert ids.index(word_start.id) < ids.index(midword.id)


def test_import_guard_blocks_dev_without_confirm() -> None:
    with pytest.raises(HTTPException) as exc:
        ensure_import_allowed(Settings(environment="development"), confirm=False)
    assert exc.value.status_code == 403


def test_import_guard_allows_with_confirm_or_production() -> None:
    # No raise in either case.
    ensure_import_allowed(Settings(environment="development"), confirm=True)
    ensure_import_allowed(Settings(environment="production"), confirm=False)


def test_cli_import_refuses_dev_without_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("TMDB_API_KEY", "x")
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli_app, ["import-catalog", "--scope", "broad"])
        assert result.exit_code == 2
        assert "Refusing to import" in result.output
    finally:
        get_settings.cache_clear()


def test_search_endpoint_returns_recommendation_items(db_session: Session) -> None:
    seed_sample_catalog(db_session)
    user = make_account(db_session)
    target = db_session.scalars(select(Title)).first()
    assert target is not None

    body = (
        authed_client(db_session, user)
        .post(f"/profiles/{user.profile.id}/catalog/search", json={"q": target.title})
        .json()
    )
    ids = {item["titleId"] for item in body["results"]}
    assert str(target.id) in ids
    assert "posterUrl" in body["results"][0]  # reuses the RecommendationItem DTO
