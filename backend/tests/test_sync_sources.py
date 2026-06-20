"""Endpoint wiring + guards for the Plex/Jellyfin sync routes (no network)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.db.base import get_session


def _client(session: Session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_plex_sync_requires_tmdb(db_session: Session) -> None:
    client = _client(db_session)
    profile_id = client.post("/profiles", json={"displayName": "me"}).json()["id"]
    response = client.post(
        "/sources/plex/sync",
        json={"profileId": profile_id, "baseUrl": "http://plex", "token": "t"},
    )
    assert response.status_code == 400
    assert "TMDB" in response.json()["detail"]


def test_jellyfin_sync_requires_tmdb(db_session: Session) -> None:
    client = _client(db_session)
    profile_id = client.post("/profiles", json={"displayName": "me"}).json()["id"]
    response = client.post(
        "/sources/jellyfin/sync",
        json={"profileId": profile_id, "baseUrl": "http://jf", "userId": "u1", "apiKey": "k"},
    )
    assert response.status_code == 400


def test_plex_sync_rejects_ssrf_base_url(db_session: Session) -> None:
    # The metadata IP must be rejected before any server-side fetch (and before the TMDB check).
    client = _client(db_session)
    profile_id = client.post("/profiles", json={"displayName": "me"}).json()["id"]
    response = client.post(
        "/sources/plex/sync",
        json={"profileId": profile_id, "baseUrl": "http://169.254.169.254/", "token": "t"},
    )
    assert response.status_code == 400
    assert "blocked" in response.json()["detail"]
