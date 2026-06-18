"""GET /profiles/{id}/sources — which services are connected and when each last synced."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.core.sync_state import set_last_synced
from phare.db.base import get_session
from phare.db.models import Profile, SourceToken


def _client(session: Session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_lists_connected_sources_excluding_internal(db_session: Session) -> None:
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()
    db_session.add_all(
        [
            SourceToken(profile_id=profile.id, source="trakt", token_encrypted="x"),
            SourceToken(profile_id=profile.id, source="trakt_refresh", token_encrypted="x"),
            SourceToken(profile_id=profile.id, source="seerr", token_encrypted="x"),
        ]
    )
    db_session.flush()
    set_last_synced(db_session, profile.id, "trakt", datetime(2026, 6, 1, tzinfo=UTC))
    db_session.flush()

    body = _client(db_session).get(f"/profiles/{profile.id}/sources").json()
    by_source = {item["source"]: item for item in body}

    assert "trakt_refresh" not in by_source  # internal token row hidden
    assert by_source["trakt"]["kind"] == "history"
    assert by_source["trakt"]["lastSyncedAt"] is not None
    assert by_source["seerr"]["kind"] == "requests"
    assert by_source["seerr"]["lastSyncedAt"] is None


def test_sources_empty_for_fresh_profile(db_session: Session) -> None:
    profile = Profile(display_name="x")
    db_session.add(profile)
    db_session.flush()
    assert _client(db_session).get(f"/profiles/{profile.id}/sources").json() == []
