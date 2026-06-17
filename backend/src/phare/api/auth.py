"""Auth endpoints: log in for a bearer token, and report auth status via ``/me``.

Both are intentionally *outside* the ``require_auth`` gate: login must be reachable to obtain a
token, and ``/me`` lets the SPA discover whether a login is needed at all.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException

from phare.api.schemas import LoginRequest, MeResponse, TokenResponse
from phare.core.auth import is_authenticated, issue_token, verify_password
from phare.core.config import get_settings

router = APIRouter(tags=["Auth"])


@router.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest) -> TokenResponse:
    settings = get_settings()
    if not settings.auth_enabled:
        raise HTTPException(status_code=400, detail="Auth is not enabled (AUTH_PASSWORD unset)")
    if not verify_password(settings, body.password):
        raise HTTPException(status_code=401, detail="Invalid password")
    return TokenResponse(token=issue_token(settings))


@router.get("/me", response_model=MeResponse)
def me(authorization: Annotated[str | None, Header()] = None) -> MeResponse:
    settings = get_settings()
    return MeResponse(
        auth_required=settings.auth_enabled,
        authenticated=not settings.auth_enabled or is_authenticated(authorization),
    )
