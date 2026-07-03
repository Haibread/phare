"""Endpoint wiring + guards for the Plex/Jellyfin sync routes (no network)."""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from phare.core.config import get_settings
from phare.providers.jellyfin import list_jellyfin_users
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


def test_capabilities_reflect_server_config(db_session: Session, monkeypatch) -> None:
    # No TMDB key → every history source is unavailable; Seerr is always offered (review D1).
    user = make_account(db_session)
    client = authed_client(db_session, user)
    body = client.get("/sources/capabilities").json()
    assert body == {"trakt": False, "plex": False, "jellyfin": False, "seerr": True}

    # TMDB set enables Plex/Jellyfin; Trakt still needs its OAuth app credentials.
    monkeypatch.setenv("TMDB_API_KEY", "t")
    get_settings.cache_clear()
    body = client.get("/sources/capabilities").json()
    assert body == {"trakt": False, "plex": True, "jellyfin": True, "seerr": True}

    monkeypatch.setenv("TRAKT_CLIENT_ID", "c")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "s")
    get_settings.cache_clear()
    assert client.get("/sources/capabilities").json()["trakt"] is True


def test_jellyfin_users_lists_names_and_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Users"
        return httpx.Response(
            200,
            json=[
                {"Id": "abc", "Name": "Alice"},
                {"Id": "def", "Name": "Bob"},
                {"Name": "no-id-skipped"},
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://jf.test")
    users = list_jellyfin_users("http://jf.test", "key", client=client)
    assert users == [{"id": "abc", "name": "Alice"}, {"id": "def", "name": "Bob"}]


def test_jellyfin_users_endpoint_rejects_ssrf(db_session: Session) -> None:
    user = make_account(db_session)
    response = authed_client(db_session, user).post(
        "/sources/jellyfin/users",
        json={"baseUrl": "http://169.254.169.254/", "apiKey": "k"},
    )
    assert response.status_code == 400
    assert "blocked" in response.json()["detail"]


def test_jellyfin_users_endpoint_returns_picker_list(db_session: Session, monkeypatch) -> None:
    user = make_account(db_session)

    def fake_list(base_url: str, api_key: str) -> list[dict[str, str]]:
        assert (base_url, api_key) == ("http://jf.test", "k")
        return [{"id": "u1", "name": "Alice"}]

    monkeypatch.setattr("phare.api.sync.list_jellyfin_users", fake_list)
    response = authed_client(db_session, user).post(
        "/sources/jellyfin/users",
        json={"baseUrl": "http://jf.test", "apiKey": "k"},
    )
    assert response.status_code == 200
    assert response.json() == [{"id": "u1", "name": "Alice"}]
