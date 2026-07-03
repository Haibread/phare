"""In-memory rate limiting: the sliding window + the middleware wiring (review I1)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.core.config import get_settings
from phare.core.ratelimit import SlidingWindowLimiter, _classify
from tests.conftest import unauthed_app


def test_sliding_window_allows_up_to_limit_then_blocks() -> None:
    lim = SlidingWindowLimiter()
    for _ in range(3):
        allowed, _ = lim.check("k", limit=3, window=60, now=100.0)
        assert allowed
    blocked, retry = lim.check("k", limit=3, window=60, now=100.0)
    assert blocked is False
    assert 0 < retry <= 60
    # Once the window has passed, the key is allowed again.
    allowed, _ = lim.check("k", limit=3, window=60, now=161.0)
    assert allowed is True


def test_classify_routes_paths_to_buckets() -> None:
    assert _classify("/auth/login", "POST") == "auth"
    assert _classify("/auth/register", "POST") == "auth"
    # The Plex device-flow poll is hit every couple of seconds by design — never throttled.
    assert _classify("/auth/plex/poll", "POST") is None
    assert _classify("/profiles/p1/chat/stream", "POST") == "chat"
    assert _classify("/catalog/import", "POST") == "import"
    assert _classify("/sources/trakt/sync", "POST") == "import"
    assert _classify("/profiles/p1/recommendations", "GET") is None


def test_auth_endpoint_is_rate_limited(monkeypatch, db_session: Session) -> None:
    monkeypatch.setenv("SECRET_KEY", "sign-me")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_PER_WINDOW", "3")
    get_settings.cache_clear()
    try:
        client = TestClient(unauthed_app(db_session))
        payload = {"email": "x@example.test", "password": "wrongpass1"}
        # The first three attempts reach the handler (401 wrong creds).
        for _ in range(3):
            assert client.post("/auth/login", json=payload).status_code == 401
        # The fourth is rejected by the limiter before the handler.
        blocked = client.post("/auth/login", json=payload)
        assert blocked.status_code == 429
        assert blocked.headers.get("retry-after")
    finally:
        get_settings.cache_clear()


def test_unlisted_endpoint_is_not_limited(monkeypatch, db_session: Session) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_PER_WINDOW", "1")
    get_settings.cache_clear()
    try:
        client = TestClient(unauthed_app(db_session))
        # /me isn't a throttled path — hitting it repeatedly never 429s.
        for _ in range(5):
            assert client.get("/me").status_code == 200
    finally:
        get_settings.cache_clear()
