"""Trakt OAuth device flow: provider polling states + the connect endpoints."""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.core.config import get_settings
from phare.core.tokens import get_source_token, store_source_token
from phare.db.models import User
from phare.providers.trakt import TraktSourceProvider
from phare.providers.trakt_oauth import PollResult, PollStatus, TraktOAuth
from tests.conftest import authed_client, make_account

_DEVICE_CODE = {
    "device_code": "dev-123",
    "user_code": "ABCD-1234",
    "verification_url": "https://trakt.tv/activate",
    "expires_in": 600,
    "interval": 5,
}


def _oauth(handler: object) -> TraktOAuth:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://trakt.test")
    return TraktOAuth(client_id="c", client_secret="s", client=client)


def test_request_device_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/device/code"
        return httpx.Response(200, json=_DEVICE_CODE)

    code = _oauth(handler).request_device_code()
    assert code.user_code == "ABCD-1234"
    assert code.verification_url == "https://trakt.tv/activate"


def test_poll_pending_then_connected() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/device/token"
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400)  # not authorized yet
        return httpx.Response(200, json={"access_token": "tok-xyz", "refresh_token": "ref"})

    oauth = _oauth(handler)
    assert oauth.poll_token("dev-123").status is PollStatus.pending
    result = oauth.poll_token("dev-123")
    assert result.status is PollStatus.connected
    assert result.access_token == "tok-xyz"


def test_poll_maps_terminal_states() -> None:
    for code, expected in [
        (410, PollStatus.expired),
        (418, PollStatus.denied),
        (429, PollStatus.slow_down),
    ]:
        result = _oauth(lambda req, c=code: httpx.Response(c)).poll_token("dev")
        assert result.status is expected
        assert result.access_token is None


# --- endpoints --------------------------------------------------------------


def _client(session: Session, user: User, monkeypatch) -> TestClient:
    monkeypatch.setenv("TRAKT_CLIENT_ID", "c")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "s")
    monkeypatch.setenv("SECRET_KEY", "sign-me")
    get_settings.cache_clear()
    return authed_client(session, user)


def test_connect_requires_credentials(db_session: Session, monkeypatch) -> None:
    monkeypatch.delenv("TRAKT_CLIENT_ID", raising=False)
    monkeypatch.delenv("TRAKT_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()
    user = make_account(db_session)
    try:
        assert (
            authed_client(db_session, user).post("/sources/trakt/connect/start").status_code == 400
        )
    finally:
        get_settings.cache_clear()


def test_connect_poll_stores_token(db_session: Session, monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_poll(self: TraktOAuth, device_code: str):  # noqa: ANN202
        captured["device_code"] = device_code
        return PollResult(
            status=PollStatus.connected, access_token="tok-xyz", refresh_token="ref-xyz"
        )

    monkeypatch.setattr(TraktOAuth, "poll_token", fake_poll)
    user = make_account(db_session)
    profile = user.profile
    client = _client(db_session, user, monkeypatch)
    try:
        response = client.post(
            "/sources/trakt/connect/poll",
            json={"profileId": str(profile.id), "deviceCode": "dev-123"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "connected"
        assert captured["device_code"] == "dev-123"
        # Both the access token and the refresh token are stored.
        settings = get_settings()
        assert get_source_token(db_session, settings, profile.id, "trakt") == "tok-xyz"
        assert get_source_token(db_session, settings, profile.id, "trakt_refresh") == "ref-xyz"
    finally:
        get_settings.cache_clear()


def test_refresh_returns_new_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token"
        body = json.loads(request.content)
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "old-ref"
        return httpx.Response(200, json={"access_token": "new-acc", "refresh_token": "new-ref"})

    result = _oauth(handler).refresh("old-ref")
    assert result.status is PollStatus.connected
    assert (result.access_token, result.refresh_token) == ("new-acc", "new-ref")


def test_sync_auto_refreshes_expired_token(db_session: Session, monkeypatch) -> None:
    monkeypatch.setenv("TRAKT_CLIENT_ID", "c")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "s")
    monkeypatch.setenv("TMDB_API_KEY", "t")
    monkeypatch.setenv("SECRET_KEY", "sign-me")
    get_settings.cache_clear()
    settings = get_settings()
    try:
        user = make_account(db_session)
        profile = user.profile
        # A stale access token + a usable refresh token.
        store_source_token(db_session, settings, profile.id, "trakt", "expired-acc")
        store_source_token(db_session, settings, profile.id, "trakt_refresh", "ref-1")

        calls = {"pull": 0}

        def fake_pull(self: TraktSourceProvider, since=None):  # noqa: ANN001, ANN202
            calls["pull"] += 1
            if calls["pull"] == 1:
                request = httpx.Request("GET", "https://trakt/sync/history")
                raise httpx.HTTPStatusError(
                    "401", request=request, response=httpx.Response(401, request=request)
                )
            return iter([])  # second attempt: authorized, nothing new

        monkeypatch.setattr(TraktSourceProvider, "pull", fake_pull)
        monkeypatch.setattr(
            TraktOAuth,
            "refresh",
            lambda self, rt: PollResult(
                status=PollStatus.connected, access_token="new-acc", refresh_token="ref-2"
            ),
        )

        response = authed_client(db_session, user).post(
            "/sources/trakt/sync", json={"profileId": str(profile.id)}
        )

        assert response.status_code == 200
        assert calls["pull"] == 2  # retried after the refresh
        assert get_source_token(db_session, settings, profile.id, "trakt") == "new-acc"
        assert get_source_token(db_session, settings, profile.id, "trakt_refresh") == "ref-2"
    finally:
        get_settings.cache_clear()


def test_sync_without_refresh_token_asks_to_reconnect(db_session: Session, monkeypatch) -> None:
    monkeypatch.setenv("TRAKT_CLIENT_ID", "c")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "s")
    monkeypatch.setenv("TMDB_API_KEY", "t")
    monkeypatch.setenv("SECRET_KEY", "sign-me")
    get_settings.cache_clear()
    settings = get_settings()
    try:
        user = make_account(db_session)
        profile = user.profile
        store_source_token(db_session, settings, profile.id, "trakt", "expired-acc")  # no refresh

        def fake_pull(self: TraktSourceProvider, since=None):  # noqa: ANN001, ANN202
            request = httpx.Request("GET", "https://trakt/sync/history")
            raise httpx.HTTPStatusError(
                "401", request=request, response=httpx.Response(401, request=request)
            )

        monkeypatch.setattr(TraktSourceProvider, "pull", fake_pull)

        response = authed_client(db_session, user).post(
            "/sources/trakt/sync", json={"profileId": str(profile.id)}
        )
        assert response.status_code == 401
        assert "reconnect" in response.json()["detail"].lower()
    finally:
        get_settings.cache_clear()
