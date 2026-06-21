"""Endpoint wiring + guards for the Plex/Jellyfin sync routes (no network)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from tests.conftest import authed_client, make_account


def test_plex_sync_requires_tmdb(db_session: Session) -> None:
    user = make_account(db_session)
    response = authed_client(db_session, user).post(
        "/sources/plex/sync",
        json={"profileId": str(user.profile.id), "baseUrl": "http://plex", "token": "t"},
    )
    assert response.status_code == 400
    assert "TMDB" in response.json()["detail"]


def test_jellyfin_sync_requires_tmdb(db_session: Session) -> None:
    user = make_account(db_session)
    response = authed_client(db_session, user).post(
        "/sources/jellyfin/sync",
        json={
            "profileId": str(user.profile.id),
            "baseUrl": "http://jf",
            "userId": "u1",
            "apiKey": "k",
        },
    )
    assert response.status_code == 400


def test_plex_sync_rejects_ssrf_base_url(db_session: Session) -> None:
    # The metadata IP must be rejected before any server-side fetch (and before the TMDB check).
    user = make_account(db_session)
    response = authed_client(db_session, user).post(
        "/sources/plex/sync",
        json={
            "profileId": str(user.profile.id),
            "baseUrl": "http://169.254.169.254/",
            "token": "t",
        },
    )
    assert response.status_code == 400
    assert "blocked" in response.json()["detail"]
