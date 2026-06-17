"""Catalog seeding (offline sample) + TMDB import upsert behaviour."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.catalog.sample import seed_sample_catalog
from phare.catalog.service import import_from_tmdb, upsert_titles
from phare.db.models import Title, TitleKind
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
