"""Trakt OAuth device flow: provider polling states + the connect endpoints."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.core.config import get_settings
from phare.core.tokens import get_source_token
from phare.db.base import get_session
from phare.db.models import Profile
from phare.providers.trakt_oauth import PollStatus, TraktOAuth

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


def _client(session: Session, monkeypatch) -> TestClient:
    monkeypatch.setenv("TRAKT_CLIENT_ID", "c")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "s")
    monkeypatch.setenv("SECRET_KEY", "sign-me")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_connect_requires_credentials(db_session: Session, monkeypatch) -> None:
    monkeypatch.delenv("TRAKT_CLIENT_ID", raising=False)
    monkeypatch.delenv("TRAKT_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        assert TestClient(app).post("/sources/trakt/connect/start").status_code == 400
    finally:
        get_settings.cache_clear()


def test_connect_poll_stores_token(db_session: Session, monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_poll(self: TraktOAuth, device_code: str):  # noqa: ANN202
        from phare.providers.trakt_oauth import PollResult

        captured["device_code"] = device_code
        return PollResult(status=PollStatus.connected, access_token="tok-xyz")

    monkeypatch.setattr(TraktOAuth, "poll_token", fake_poll)
    client = _client(db_session, monkeypatch)
    try:
        profile = Profile(display_name="me")
        db_session.add(profile)
        db_session.flush()

        response = client.post(
            "/sources/trakt/connect/poll",
            json={"profileId": str(profile.id), "deviceCode": "dev-123"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "connected"
        assert captured["device_code"] == "dev-123"
        assert get_source_token(db_session, get_settings(), profile.id, "trakt") == "tok-xyz"
    finally:
        get_settings.cache_clear()
