"""Multi-user auth: tokens, local accounts, Sign in with Plex, and enforced isolation.

The suite stays hermetic — no real Plex. ``FakeAuthProvider`` (overriding the Plex provider
dependency) supplies canned identities + server membership. See ``docs/auth.md``.
"""

from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.auth import get_plex_auth_provider
from phare.auth.provider import AuthStatus, ResolvedIdentity
from phare.core.auth import hash_password, issue_token, resolve_user, verify_password
from phare.core.config import Settings, get_settings
from phare.core.crypto import decrypt, encrypt
from phare.core.tokens import get_source_token, store_source_token
from phare.providers.fakes import FakeAuthProvider
from tests.conftest import make_account, unauthed_app


def _with_settings(monkeypatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


def _plex_client(session: Session, identity: ResolvedIdentity, *, status=AuthStatus.authorized):
    """A client whose Plex auth provider is a fake yielding ``identity``."""
    app = unauthed_app(session)
    app.dependency_overrides[get_plex_auth_provider] = lambda: FakeAuthProvider(
        identity, status=status
    )
    return TestClient(app)


def _identity(
    subject: str, *, servers: tuple[str, ...], email: str | None = None
) -> ResolvedIdentity:
    return ResolvedIdentity(
        provider="plex",
        subject=subject,
        display_name=f"Plex {subject}",
        email=email,
        access_token=f"tok-{subject}",
        server_ids=servers,
    )


# --- token + password primitives -------------------------------------------


def test_token_resolves_user_expires_and_revokes(db_session: Session) -> None:
    settings = Settings(secret_key="sign-me")
    user = make_account(db_session)
    token = issue_token(settings, user.id, user.token_version)
    assert resolve_user(db_session, settings, f"Bearer {token}") == user
    # Tampered signature -> None.
    assert resolve_user(db_session, settings, f"Bearer {token}x") is None
    # Expired -> None.
    expired = issue_token(
        settings,
        user.id,
        user.token_version,
        now=time.time() - settings.auth_token_ttl_seconds - 10,
    )
    assert resolve_user(db_session, settings, f"Bearer {expired}") is None
    # No secret -> can't verify.
    assert resolve_user(db_session, Settings(secret_key=None), f"Bearer {token}") is None
    # Bumping the user's token version revokes the previously issued token (review I3).
    user.token_version += 1
    db_session.flush()
    assert resolve_user(db_session, settings, f"Bearer {token}") is None


def test_password_hash_roundtrip() -> None:
    stored = hash_password("correct horse battery staple")
    assert stored != "correct horse battery staple"  # never plaintext
    assert verify_password(stored, "correct horse battery staple") is True
    assert verify_password(stored, "wrong") is False


def test_encryption_roundtrip_and_wrong_secret() -> None:
    a = Settings(secret_key="key-a")
    b = Settings(secret_key="key-b")
    cipher = encrypt(a, "trakt-token")
    assert decrypt(a, cipher) == "trakt-token"
    assert decrypt(b, cipher) is None  # wrong secret -> None, not a crash


# --- secure by default ------------------------------------------------------


def test_closed_by_default_and_reports_setup(monkeypatch, db_session: Session) -> None:
    _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        client = TestClient(unauthed_app(db_session))
        # No token -> every data endpoint is closed.
        assert client.get("/profiles").status_code == 401
        body = client.get("/me").json()
        assert body == {
            "needsSetup": True,
            "registrationOpen": False,
            "authenticated": False,
            "user": None,
        }
    finally:
        get_settings.cache_clear()


# --- local accounts ---------------------------------------------------------


def test_first_register_becomes_admin_and_logs_in(monkeypatch, db_session: Session) -> None:
    _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        client = TestClient(unauthed_app(db_session))
        resp = client.post(
            "/auth/register",
            json={"email": "owner@example.test", "password": "hunter2pass", "displayName": "Owner"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["token"] is not None
        assert body["user"]["isAdmin"] is True
        assert body["user"]["profileId"]  # a profile was created with the account

        headers = {"Authorization": f"Bearer {body['token']}"}
        me = client.get("/me", headers=headers).json()
        assert me["authenticated"] is True
        assert me["needsSetup"] is False
        # The token grants access to the account's own profile listing.
        profiles = client.get("/profiles", headers=headers).json()
        assert [p["id"] for p in profiles["items"]] == [body["user"]["profileId"]]
    finally:
        get_settings.cache_clear()


def test_registration_closed_for_second_user(monkeypatch, db_session: Session) -> None:
    _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        make_account(db_session, email="owner@example.test")  # an account already exists
        client = TestClient(unauthed_app(db_session))
        resp = client.post(
            "/auth/register",
            json={"email": "intruder@example.test", "password": "longenough", "displayName": "X"},
        )
        assert resp.status_code == 403
    finally:
        get_settings.cache_clear()


def test_open_registration_allows_self_signup(monkeypatch, db_session: Session) -> None:
    _with_settings(monkeypatch, SECRET_KEY="sign-me", REGISTRATION_OPEN="true")
    try:
        make_account(db_session, email="owner@example.test")
        client = TestClient(unauthed_app(db_session))
        resp = client.post(
            "/auth/register",
            json={"email": "newbie@example.test", "password": "longenough", "displayName": "New"},
        )
        assert resp.status_code == 201
        assert resp.json()["user"]["isAdmin"] is False
    finally:
        get_settings.cache_clear()


def test_login_rejects_wrong_password(monkeypatch, db_session: Session) -> None:
    _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        client = TestClient(unauthed_app(db_session))
        client.post(
            "/auth/register",
            json={"email": "owner@example.test", "password": "rightpass1", "displayName": "Owner"},
        )
        assert (
            client.post(
                "/auth/login", json={"email": "owner@example.test", "password": "wrongpass"}
            ).status_code
            == 401
        )
        ok = client.post(
            "/auth/login", json={"email": "owner@example.test", "password": "rightpass1"}
        )
        assert ok.status_code == 200
        assert ok.json()["token"]
    finally:
        get_settings.cache_clear()


def _register(client: TestClient, email: str, password: str) -> str:
    """Register a first (admin) account and return its bearer token."""
    resp = client.post(
        "/auth/register",
        json={"email": email, "password": password, "displayName": "Owner"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def test_password_policy_rejects_short_passwords(monkeypatch, db_session: Session) -> None:
    # I4: length is the rule — 9 chars is refused, 10 accepted.
    _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        client = TestClient(unauthed_app(db_session))
        short = client.post(
            "/auth/register",
            json={"email": "a@example.test", "password": "9charsxxx", "displayName": "A"},
        )
        assert short.status_code == 422
        ok = client.post(
            "/auth/register",
            json={"email": "a@example.test", "password": "tencharsok", "displayName": "A"},
        )
        assert ok.status_code == 201
    finally:
        get_settings.cache_clear()


def test_logout_revokes_the_token(monkeypatch, db_session: Session) -> None:
    # I3: a stateless token can't be individually revoked, so logout bumps the version and every
    # token issued before stops working.
    _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        client = TestClient(unauthed_app(db_session))
        token = _register(client, "owner@example.test", "rightpass1")
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/me", headers=headers).json()["authenticated"] is True
        assert client.post("/auth/logout", headers=headers).status_code == 204
        # The same token is now dead.
        assert client.get("/me", headers=headers).json()["authenticated"] is False
    finally:
        get_settings.cache_clear()


def test_change_password_revokes_old_sessions_and_returns_fresh_token(
    monkeypatch, db_session: Session
) -> None:
    _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        client = TestClient(unauthed_app(db_session))
        token = _register(client, "owner@example.test", "oldpassword")
        headers = {"Authorization": f"Bearer {token}"}

        # Wrong current password is refused.
        assert (
            client.post(
                "/auth/password",
                headers=headers,
                json={"currentPassword": "nope", "newPassword": "newpassword1"},
            ).status_code
            == 401
        )
        # Correct current password rotates it and hands back a working token.
        changed = client.post(
            "/auth/password",
            headers=headers,
            json={"currentPassword": "oldpassword", "newPassword": "newpassword1"},
        )
        assert changed.status_code == 200
        fresh = {"Authorization": f"Bearer {changed.json()['token']}"}
        assert client.get("/me", headers=fresh).json()["authenticated"] is True
        # The pre-change token is revoked, and the new password logs in.
        assert client.get("/me", headers=headers).json()["authenticated"] is False
        assert (
            client.post(
                "/auth/login", json={"email": "owner@example.test", "password": "newpassword1"}
            ).status_code
            == 200
        )
    finally:
        get_settings.cache_clear()


def test_admin_reset_password(monkeypatch, db_session: Session) -> None:
    _with_settings(monkeypatch, SECRET_KEY="sign-me", REGISTRATION_OPEN="true")
    try:
        client = TestClient(unauthed_app(db_session))
        admin_token = _register(client, "owner@example.test", "adminpass1")
        member = client.post(
            "/auth/register",
            json={"email": "member@example.test", "password": "memberpass1", "displayName": "M"},
        ).json()
        member_id = member["user"]["id"]
        member_token = {"Authorization": f"Bearer {member['token']}"}

        # A non-admin can't reset anyone.
        assert (
            client.post(
                f"/auth/admin/users/{member_id}/reset-password", headers=member_token
            ).status_code
            == 403
        )
        # The admin resets the member: sessions revoked, temp password returned + usable.
        reset = client.post(
            f"/auth/admin/users/{member_id}/reset-password",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert reset.status_code == 200
        temp = reset.json()["temporaryPassword"]
        assert client.get("/me", headers=member_token).json()["authenticated"] is False
        assert (
            client.post(
                "/auth/login", json={"email": "member@example.test", "password": temp}
            ).status_code
            == 200
        )
    finally:
        get_settings.cache_clear()


# --- Sign in with Plex ------------------------------------------------------


def test_plex_first_signin_becomes_owner(monkeypatch, db_session: Session) -> None:
    _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        client = _plex_client(db_session, _identity("owner", servers=("srv-A",), email="o@x.test"))
        resp = client.post("/auth/plex/poll", json={"challengeId": "pin"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "authorized"
        token = body["token"]
        me = client.get("/me", headers={"Authorization": f"Bearer {token}"}).json()
        assert me["user"]["isAdmin"] is True  # first sign-in is the owner/admin
        # The Plex token was stored for ingestion (login also connects the source).
        owner_profile = uuid.UUID(me["user"]["profileId"])
        assert get_source_token(db_session, get_settings(), owner_profile, "plex") == "tok-owner"
    finally:
        get_settings.cache_clear()


def test_plex_member_allowed_stranger_refused(monkeypatch, db_session: Session) -> None:
    _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        # Owner binds srv-A.
        _plex_client(db_session, _identity("owner", servers=("srv-A",))).post(
            "/auth/plex/poll", json={"challengeId": "pin"}
        )
        # A friend who shares srv-A is let in (but isn't admin).
        member = _plex_client(db_session, _identity("friend", servers=("srv-A", "srv-B")))
        resp = member.post("/auth/plex/poll", json={"challengeId": "pin"})
        assert resp.status_code == 200
        token = resp.json()["token"]
        assert (
            member.get("/me", headers={"Authorization": f"Bearer {token}"}).json()["user"][
                "isAdmin"
            ]
            is False
        )
        # A stranger on an unrelated server is refused.
        stranger = _plex_client(db_session, _identity("stranger", servers=("srv-Z",)))
        assert stranger.post("/auth/plex/poll", json={"challengeId": "pin"}).status_code == 403
    finally:
        get_settings.cache_clear()


def test_plex_poll_pending_returns_status(monkeypatch, db_session: Session) -> None:
    _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        client = _plex_client(
            db_session, _identity("owner", servers=("srv-A",)), status=AuthStatus.pending
        )
        resp = client.post("/auth/plex/poll", json={"challengeId": "pin"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "pending", "token": None}
    finally:
        get_settings.cache_clear()


# --- isolation --------------------------------------------------------------


def test_one_user_cannot_reach_anothers_profile(monkeypatch, db_session: Session) -> None:
    _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        alice = make_account(db_session, display_name="alice", email="alice@example.test")
        bob = make_account(db_session, display_name="bob", email="bob@example.test")
        client = TestClient(unauthed_app(db_session))
        headers = {
            "Authorization": f"Bearer {issue_token(get_settings(), alice.id, alice.token_version)}"
        }

        # Alice sees her own profile only.
        items = client.get("/profiles", headers=headers).json()["items"]
        assert [p["id"] for p in items] == [str(alice.profile.id)]
        # Bob's profile is a 404 for Alice, across every profile-scoped surface.
        bob_id = bob.profile.id
        assert (
            client.get("/history", params={"profileId": str(bob_id)}, headers=headers).status_code
            == 404
        )
        assert client.get(f"/profiles/{bob_id}/taste", headers=headers).status_code == 404
        assert client.get(f"/profiles/{bob_id}/memory", headers=headers).status_code == 404
    finally:
        get_settings.cache_clear()


def test_stored_token_encrypts_and_reads_back(monkeypatch, db_session: Session) -> None:
    settings = _with_settings(monkeypatch, SECRET_KEY="sign-me")
    try:
        user = make_account(db_session)
        assert store_source_token(db_session, settings, user.profile.id, "trakt", "abc123") is True
        assert get_source_token(db_session, settings, user.profile.id, "trakt") == "abc123"
    finally:
        get_settings.cache_clear()
