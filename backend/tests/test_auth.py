"""Opt-in auth: open passthrough when unconfigured, enforced when AUTH_PASSWORD is set."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.core.auth import issue_token, verify_token
from phare.core.config import Settings, get_settings
from phare.core.crypto import decrypt, encrypt
from phare.core.tokens import get_source_token, store_source_token
from phare.db.base import get_session
from phare.db.models import Profile


def _client(session: Session, settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _with_settings(monkeypatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


# --- token primitives -------------------------------------------------------


def test_token_roundtrip_and_expiry() -> None:
    settings = Settings(auth_password="hunter2")
    token = issue_token(settings)
    assert verify_token(settings, token)
    # Tampered signature is rejected.
    assert not verify_token(settings, token + "x")
    # Expired token is rejected.
    expired = issue_token(settings, now=time.time() - settings.auth_token_ttl_seconds - 10)
    assert not verify_token(settings, expired)


def test_encryption_roundtrip_and_wrong_secret() -> None:
    a = Settings(secret_key="key-a")
    b = Settings(secret_key="key-b")
    cipher = encrypt(a, "trakt-token")
    assert decrypt(a, cipher) == "trakt-token"
    assert decrypt(b, cipher) is None  # wrong secret -> None, not a crash


# --- open mode (no AUTH_PASSWORD) -------------------------------------------


def test_open_mode_allows_unauthenticated(monkeypatch, db_session: Session) -> None:
    settings = _with_settings(monkeypatch, AUTH_PASSWORD="")
    assert settings.auth_enabled is False
    client = _client(db_session, settings)
    assert client.get("/profiles").status_code == 200
    assert client.get("/me").json() == {"authRequired": False, "authenticated": True}
    get_settings.cache_clear()


# --- enforced mode ----------------------------------------------------------


def test_enforced_mode_requires_token(monkeypatch, db_session: Session) -> None:
    _with_settings(monkeypatch, AUTH_PASSWORD="hunter2", SECRET_KEY="sign-me")
    client = _client(db_session, get_settings())
    try:
        assert client.get("/profiles").status_code == 401
        assert client.get("/me").json()["authRequired"] is True

        assert client.post("/auth/login", json={"password": "wrong"}).status_code == 401
        token = client.post("/auth/login", json={"password": "hunter2"}).json()["token"]

        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/profiles", headers=headers).status_code == 200
        assert client.get("/me", headers=headers).json()["authenticated"] is True
    finally:
        get_settings.cache_clear()


def test_stored_token_encrypts_and_reads_back(monkeypatch, db_session: Session) -> None:
    settings = _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        profile = Profile(display_name="me")
        db_session.add(profile)
        db_session.flush()

        assert store_source_token(db_session, settings, profile.id, "trakt", "abc123") is True
        assert get_source_token(db_session, settings, profile.id, "trakt") == "abc123"
    finally:
        get_settings.cache_clear()
