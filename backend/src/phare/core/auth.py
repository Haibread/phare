"""Multi-user auth: identity-bearing tokens, password hashing, and the current-user dependency.

Tokens are stateless and signed: ``<user_id>.<expiry>.<hmac_sha256>``, where the HMAC (keyed with
``SECRET_KEY``) covers the user id and expiry — so the backend can resolve a token to *which* user
without a session store, and every data endpoint can isolate by identity. There is no open mode:
without a valid token, guarded endpoints return 401. See ``docs/auth.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from phare.core.config import Settings, get_settings
from phare.db.base import get_session
from phare.db.models import User

logger = logging.getLogger(__name__)

# argon2id with the library defaults — a sensible memory/time cost for interactive logins.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a plaintext password with argon2id. The result embeds its own parameters + salt."""
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """True iff ``password`` matches ``stored_hash``. Constant-time within argon2."""
    try:
        return _hasher.verify(stored_hash, password)
    except VerifyMismatchError:
        return False


def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def issue_token(settings: Settings, user_id: uuid.UUID, *, now: float | None = None) -> str:
    """Mint a signed bearer token carrying the user id. Caller must have authenticated the user."""
    secret = settings.signing_secret
    if secret is None:
        raise RuntimeError("SECRET_KEY must be set to issue tokens")
    expiry = int((now or time.time()) + settings.auth_token_ttl_seconds)
    payload = f"{user_id}:{expiry}"
    return f"{user_id}.{expiry}.{_sign(secret, payload)}"


def verify_token(settings: Settings, token: str, *, now: float | None = None) -> uuid.UUID | None:
    """Return the token's user id iff it is well-formed, correctly signed, and unexpired."""
    secret = settings.signing_secret
    if secret is None:
        return None
    user_id_str, _, rest = token.partition(".")
    expiry_str, _, signature = rest.partition(".")
    if not signature or not expiry_str.isdigit():
        return None
    expected = _sign(secret, f"{user_id_str}:{expiry_str}")
    if not hmac.compare_digest(expected, signature):
        return None
    if int(expiry_str) <= (now or time.time()):
        return None
    try:
        return uuid.UUID(user_id_str)
    except ValueError:
        return None


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[len("bearer ") :].strip()
    return None


def resolve_user(session: Session, settings: Settings, authorization: str | None) -> User | None:
    """Resolve a request's bearer token to its :class:`User`, or ``None`` if not authenticated."""
    token = _bearer(authorization)
    if token is None:
        return None
    user_id = verify_token(settings, token)
    if user_id is None:
        return None
    return session.get(User, user_id)


def get_current_user(
    session: Annotated[Session, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """FastAPI dependency: the authenticated user, or 401. Use as a router guard or to read it."""
    user = resolve_user(session, get_settings(), authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_own_profile(user: User, profile_id: uuid.UUID) -> None:
    """Enforce per-user isolation: 404 unless ``profile_id`` is the caller's own profile.

    404 (not 403) so a probe can't confirm another user's profile exists. Strict 1:1 makes this a
    single comparison — a user has exactly one profile. See ``docs/auth.md``.
    """
    if user.profile is None or user.profile.id != profile_id:
        raise HTTPException(status_code=404, detail="Profile not found")


# Convenience for wiring router-level auth without importing the function inline at each site.
AuthDependency = Depends(get_current_user)
