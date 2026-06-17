"""Opt-in instance auth (single-user, self-hosted).

When ``AUTH_PASSWORD`` is unset the API is open — exactly the prior dev posture, so existing
tests and E2E keep passing. When set, every data endpoint requires a bearer token minted by
``POST /auth/login``. Tokens are stateless: ``<expiry>.<hmac>`` signed with the instance secret,
so there's no session store to manage.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from phare.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def issue_token(settings: Settings, *, now: float | None = None) -> str:
    """Mint a signed bearer token. Caller must have already verified the password."""
    secret = settings.signing_secret
    if secret is None:  # pragma: no cover - guarded by auth_enabled at the call site
        raise RuntimeError("cannot issue a token without a signing secret")
    expiry = int((now or time.time()) + settings.auth_token_ttl_seconds)
    return f"{expiry}.{_sign(secret, f'phare-auth:{expiry}')}"


def verify_token(settings: Settings, token: str, *, now: float | None = None) -> bool:
    """True iff the token is well-formed, correctly signed, and unexpired."""
    secret = settings.signing_secret
    if secret is None:
        return False
    expiry_str, _, signature = token.partition(".")
    if not signature or not expiry_str.isdigit():
        return False
    expected = _sign(secret, f"phare-auth:{expiry_str}")
    if not hmac.compare_digest(expected, signature):
        return False
    return int(expiry_str) > (now or time.time())


def verify_password(settings: Settings, password: str) -> bool:
    """Constant-time password check against the configured ``AUTH_PASSWORD``."""
    if settings.auth_password is None:
        return False
    return hmac.compare_digest(settings.auth_password, password)


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[len("bearer ") :].strip()
    return None


def require_auth(authorization: Annotated[str | None, Header()] = None) -> None:
    """FastAPI dependency: no-op when auth is disabled, else enforces a valid bearer token."""
    settings = get_settings()
    if not settings.auth_enabled:
        return
    token = _bearer(authorization)
    if token is None or not verify_token(settings, token):
        raise HTTPException(status_code=401, detail="Authentication required")


def is_authenticated(authorization: str | None) -> bool:
    """Whether a request carries a currently-valid token (for the ``/me`` status endpoint)."""
    settings = get_settings()
    token = _bearer(authorization)
    return token is not None and verify_token(settings, token)


# Convenience for routers that want to require auth without importing the function inline.
AuthDependency = Depends(require_auth)
