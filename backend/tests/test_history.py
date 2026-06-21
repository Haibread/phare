"""History endpoint: pagination, mapping, and per-profile isolation."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from phare.db.models import EventType, TitleKind
from phare.ingest.service import IngestionService
from phare.providers.fakes import FakeMetadataProvider
from phare.providers.types import RawEvent, RawMediaType, TitleMetadata
from tests.conftest import authed_client, make_account

MOVIE = TitleMetadata(kind=TitleKind.movie, tmdb_id=438631, title="Dune", year=2021)


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
    user = make_account(db_session)
    profile_id = user.profile.id
    events = [_movie_event(f"history:{i}", datetime(2024, 1, i, tzinfo=UTC)) for i in range(1, 4)]
    IngestionService(
        db_session, FakeMetadataProvider(titles={(438631, TitleKind.movie): MOVIE})
    ).ingest(profile_id, events)
    db_session.flush()

    response = authed_client(db_session, user).get(
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
    mine = make_account(db_session, display_name="me", email="me@example.test")
    other = make_account(db_session, display_name="other", email="other@example.test")
    IngestionService(db_session, metadata).ingest(
        mine.profile.id, [_movie_event("history:1", datetime(2024, 1, 1, tzinfo=UTC))]
    )
    IngestionService(db_session, metadata).ingest(
        other.profile.id, [_movie_event("history:2", datetime(2024, 2, 1, tzinfo=UTC))]
    )
    db_session.flush()

    client = authed_client(db_session, mine)
    body = client.get("/history", params={"profileId": str(mine.profile.id)}).json()
    assert body["total"] == 1
    assert all(item["source"] == "trakt" for item in body["items"])
    assert len(body["items"]) == 1
    # Another user's profile is invisible — a 404, not a filtered 200.
    leak = client.get("/history", params={"profileId": str(other.profile.id)})
    assert leak.status_code == 404
