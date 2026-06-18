"""Catalog seeding (offline sample) + TMDB import upsert behaviour."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.catalog.sample import seed_sample_catalog
from phare.catalog.service import import_from_tmdb, search_titles, upsert_titles
from phare.db.base import get_session
from phare.db.models import Profile, Title, TitleKind
from phare.providers.types import TitleMetadata


def _count_titles(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Title)) or 0


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


def _client(session: Session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_search_endpoint_returns_recommendation_items(db_session: Session) -> None:
    seed_sample_catalog(db_session)
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()
    target = db_session.scalars(select(Title)).first()
    assert target is not None

    body = (
        _client(db_session)
        .post(f"/profiles/{profile.id}/catalog/search", json={"q": target.title})
        .json()
    )
    ids = {item["titleId"] for item in body["results"]}
    assert str(target.id) in ids
    assert "posterUrl" in body["results"][0]  # reuses the RecommendationItem DTO
