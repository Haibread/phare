"""History endpoint: pagination, mapping, and per-profile isolation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.db.base import get_session
from phare.db.models import EventType, Profile, TitleKind
from phare.ingest.service import IngestionService
from phare.providers.fakes import FakeMetadataProvider
from phare.providers.types import RawEvent, RawMediaType, TitleMetadata

MOVIE = TitleMetadata(kind=TitleKind.movie, tmdb_id=438631, title="Dune", year=2021)


def _client(session: Session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _profile(session: Session, name: str) -> uuid.UUID:
    profile = Profile(display_name=name)
    session.add(profile)
    session.flush()
    return profile.id


def _movie_event(ref: str, when: datetime) -> RawEvent:
    return RawEvent(
        source="trakt",
        media_type=RawMediaType.movie,
        type=EventType.watched,
        tmdb_id=438631,
        occurred_at=when,
        external_ref=ref,
    )


def test_history_pagination(db_session: Session) -> None:
    profile_id = _profile(db_session, "me")
    events = [_movie_event(f"history:{i}", datetime(2024, 1, i, tzinfo=UTC)) for i in range(1, 4)]
    IngestionService(
        db_session, FakeMetadataProvider(titles={(438631, TitleKind.movie): MOVIE})
    ).ingest(profile_id, events)
    db_session.flush()

    response = _client(db_session).get(
        "/history", params={"profileId": str(profile_id), "perPage": 2}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["perPage"] == 2
    assert len(body["items"]) == 2
    # Most recent first.
    assert body["items"][0]["title"] == "Dune"
    assert body["items"][0]["type"] == "watched"


def test_history_is_isolated_per_profile(db_session: Session) -> None:
    metadata = FakeMetadataProvider(titles={(438631, TitleKind.movie): MOVIE})
    mine = _profile(db_session, "me")
    other = _profile(db_session, "other")
    IngestionService(db_session, metadata).ingest(
        mine, [_movie_event("history:1", datetime(2024, 1, 1, tzinfo=UTC))]
    )
    IngestionService(db_session, metadata).ingest(
        other, [_movie_event("history:2", datetime(2024, 2, 1, tzinfo=UTC))]
    )
    db_session.flush()

    body = _client(db_session).get("/history", params={"profileId": str(mine)}).json()
    assert body["total"] == 1
    assert all(item["source"] == "trakt" for item in body["items"])
    # The other profile's event must not leak in.
    assert len(body["items"]) == 1
