"""Ingestion: resolution, TV tree, idempotency, conflict, import cleanup."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.db.models import Episode, EventType, Profile, Season, Title, TitleKind, WatchEvent
from phare.ingest.service import IngestionService, exclude_events
from phare.providers.fakes import FakeMetadataProvider
from phare.providers.types import ExternalMatch, RawEvent, RawMediaType, TitleMetadata

MOVIE = TitleMetadata(kind=TitleKind.movie, tmdb_id=438631, title="Dune", year=2021)
SHOW = TitleMetadata(kind=TitleKind.show, tmdb_id=95396, title="Severance", year=2022)


def _metadata() -> FakeMetadataProvider:
    return FakeMetadataProvider(
        titles={
            (438631, TitleKind.movie): MOVIE,
            (95396, TitleKind.show): SHOW,
        }
    )


def _profile(session: Session) -> uuid.UUID:
    profile = Profile(display_name="me")
    session.add(profile)
    session.flush()
    return profile.id


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_same_tmdb_id_movie_and_show_are_distinct_titles(db_session: Session) -> None:
    # TMDB's movie and TV id spaces overlap: 1398 is *Stalker* (movie) AND *The Sopranos* (show). A
    # kind-less lookup + column-level UNIQUE(tmdb_id) collapsed them onto one row, so a "movie 1398"
    # event attached to the show (review H3a). They must now be two distinct titles.
    profile_id = _profile(db_session)
    metadata = FakeMetadataProvider(
        titles={
            (1398, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie, tmdb_id=1398, title="Stalker", year=1979
            ),
            (1398, TitleKind.show): TitleMetadata(
                kind=TitleKind.show, tmdb_id=1398, title="The Sopranos", year=1999
            ),
        }
    )
    svc = IngestionService(db_session, metadata)
    # Seed the show first (a kind-less lookup would wrongly find it), then a MOVIE event for 1398.
    svc.ingest(
        profile_id,
        [
            RawEvent(
                source="s",
                media_type=RawMediaType.show,
                type=EventType.watched,
                tmdb_id=1398,
                external_ref="e:show",
            )
        ],
    )
    svc.ingest(
        profile_id,
        [
            RawEvent(
                source="s",
                media_type=RawMediaType.movie,
                type=EventType.watched,
                tmdb_id=1398,
                external_ref="e:movie",
            )
        ],
    )
    titles = db_session.scalars(select(Title).where(Title.tmdb_id == 1398)).all()
    assert {t.kind for t in titles} == {TitleKind.movie, TitleKind.show}  # two rows, not one
    movie = next(t for t in titles if t.kind is TitleKind.movie)
    movie_event = db_session.scalar(select(WatchEvent).where(WatchEvent.external_ref == "e:movie"))
    assert movie_event is not None and movie_event.title_id == movie.id  # attached to the movie


def test_movie_creates_title_and_event(db_session: Session) -> None:
    profile_id = _profile(db_session)
    event = RawEvent(
        source="trakt",
        media_type=RawMediaType.movie,
        type=EventType.watched,
        tmdb_id=438631,
        occurred_at=datetime(2024, 1, 1, tzinfo=UTC),
        external_ref="history:1",
    )
    result = IngestionService(db_session, _metadata()).ingest(profile_id, [event])

    assert result.created == 1
    assert result.titles_created == 1
    title = db_session.scalar(select(Title).where(Title.tmdb_id == 438631))
    assert title is not None and title.title == "Dune"


def test_episode_builds_tv_tree(db_session: Session) -> None:
    profile_id = _profile(db_session)
    event = RawEvent(
        source="trakt",
        media_type=RawMediaType.episode,
        type=EventType.watched,
        tmdb_id=95396,
        season_number=2,
        episode_number=5,
        external_ref="history:2",
    )
    IngestionService(db_session, _metadata()).ingest(profile_id, [event])

    assert _count(db_session, Title) == 1
    assert _count(db_session, Season) == 1
    assert _count(db_session, Episode) == 1
    watch = db_session.scalar(select(WatchEvent))
    assert watch is not None
    assert watch.season_id is not None
    assert watch.episode_id is not None


def test_reingest_is_idempotent(db_session: Session) -> None:
    profile_id = _profile(db_session)
    event = RawEvent(
        source="trakt",
        media_type=RawMediaType.movie,
        type=EventType.watched,
        tmdb_id=438631,
        occurred_at=datetime(2024, 1, 1, tzinfo=UTC),
        external_ref="history:1",
    )
    service = IngestionService(db_session, _metadata())
    service.ingest(profile_id, [event])
    second = service.ingest(profile_id, [event])

    assert second.created == 0
    assert second.skipped == 1
    assert _count(db_session, WatchEvent) == 1


def test_rating_conflict_most_recent_wins(db_session: Session) -> None:
    profile_id = _profile(db_session)
    early = RawEvent(
        source="trakt",
        media_type=RawMediaType.movie,
        type=EventType.rated,
        tmdb_id=438631,
        rating=6,
        occurred_at=datetime(2024, 1, 1, tzinfo=UTC),
        external_ref="rating:movie:9",
    )
    late = early.model_copy(update={"rating": 9, "occurred_at": datetime(2024, 6, 1, tzinfo=UTC)})
    service = IngestionService(db_session, _metadata())
    service.ingest(profile_id, [early])
    service.ingest(profile_id, [late])

    watch = db_session.scalar(select(WatchEvent))
    assert watch is not None
    assert float(watch.rating) == 9.0

    # An older incoming rating does not overwrite.
    stale = early.model_copy(update={"rating": 3, "occurred_at": datetime(2023, 1, 1, tzinfo=UTC)})
    service.ingest(profile_id, [stale])
    db_session.expire_all()
    watch = db_session.scalar(select(WatchEvent))
    assert watch is not None
    assert float(watch.rating) == 9.0


def test_imdb_resolution(db_session: Session) -> None:
    profile_id = _profile(db_session)
    metadata = FakeMetadataProvider(
        titles={(438631, TitleKind.movie): MOVIE},
        imdb={"tt1160419": ExternalMatch(tmdb_id=438631, kind=TitleKind.movie)},
    )
    event = RawEvent(
        source="trakt",
        media_type=RawMediaType.movie,
        type=EventType.watched,
        imdb_id="tt1160419",
        external_ref="history:1",
    )
    result = IngestionService(db_session, metadata).ingest(profile_id, [event])

    assert result.created == 1
    assert _count(db_session, Title) == 1


def test_unresolvable_event_skipped(db_session: Session) -> None:
    profile_id = _profile(db_session)
    event = RawEvent(
        source="trakt",
        media_type=RawMediaType.movie,
        type=EventType.watched,
        tmdb_id=999999,  # not in metadata
        external_ref="history:1",
    )
    result = IngestionService(db_session, _metadata()).ingest(profile_id, [event])

    assert result.skipped == 1
    assert _count(db_session, Title) == 0
    assert _count(db_session, WatchEvent) == 0


def test_import_cleanup_excludes_by_date(db_session: Session) -> None:
    profile_id = _profile(db_session)
    old = RawEvent(
        source="trakt",
        media_type=RawMediaType.movie,
        type=EventType.watched,
        tmdb_id=438631,
        occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
        external_ref="history:old",
    )
    recent = RawEvent(
        source="trakt",
        media_type=RawMediaType.show,
        type=EventType.rated,
        tmdb_id=95396,
        rating=8,
        occurred_at=datetime(2024, 1, 1, tzinfo=UTC),
        external_ref="rating:show:7",
    )
    IngestionService(db_session, _metadata()).ingest(profile_id, [old, recent])

    excluded = exclude_events(db_session, profile_id, before=datetime(2022, 1, 1, tzinfo=UTC))
    db_session.flush()
    assert excluded == 1
    rows = db_session.scalars(select(WatchEvent).where(WatchEvent.excluded.is_(True))).all()
    assert len(rows) == 1
    assert rows[0].external_ref == "history:old"
