"""Auth endpoints: register / log in for a bearer token, "Sign in with Plex", and ``/me``.

All are intentionally *outside* the ``get_current_user`` gate: they must be reachable without a
token to obtain one (``/me`` also lets the SPA discover first-run vs. login). See ``docs/auth.md``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from phare.api.schemas import (
    AdminResetPasswordResponse,
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    PlexPollRequest,
    PlexPollResponse,
    PlexStartResponse,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserResponse,
)
from phare.auth.plex import PlexAuthProvider, derive_client_identifier
from phare.auth.provider import AuthProvider, AuthStatus
from phare.auth.service import (
    AuthorizationError,
    RegistrationError,
    authenticate_local,
    change_local_password,
    is_first_user,
    provision_from_identity,
    register_local,
    reset_local_password,
)
from phare.core.auth import get_current_user, issue_token, resolve_user
from phare.core.config import Settings, get_settings
from phare.db.base import get_session
from phare.db.models import User

router = APIRouter(tags=["Auth"])


def get_plex_auth_provider() -> AuthProvider:
    """The configured Plex auth provider. Overridden in tests with a fake (no live Plex)."""
    settings = get_settings()
    seed = settings.signing_secret or settings.plex_product_name
    client_id = settings.plex_client_identifier or derive_client_identifier(seed)
    return PlexAuthProvider(client_identifier=client_id, product=settings.plex_product_name)


def _require_secret(settings: Settings) -> None:
    if settings.signing_secret is None:
        raise HTTPException(status_code=500, detail="SECRET_KEY is not configured")


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        profile_id=user.profile.id,
    )


@router.post("/auth/register", response_model=RegisterResponse, status_code=201)
def register(
    body: RegisterRequest,
    session: Annotated[Session, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> RegisterResponse:
    """Create a local account. The first account is admin; afterwards it's admin-created unless
    ``REGISTRATION_OPEN`` is set."""
    settings = get_settings()
    _require_secret(settings)
    first = is_first_user(session)
    caller = resolve_user(session, settings, authorization)
    if not first and not settings.registration_open and not (caller and caller.is_admin):
        raise HTTPException(status_code=403, detail="Registration is closed")
    try:
        user = register_local(session, body.email, body.password, body.display_name, is_admin=first)
    except RegistrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    # Self-registration (no authenticated caller) logs you straight in; an admin creating an
    # account for someone else gets the user back without a token.
    token = issue_token(settings, user.id, user.token_version) if caller is None else None
    return RegisterResponse(token=token, user=_user_response(user))


@router.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, session: Annotated[Session, Depends(get_session)]) -> TokenResponse:
    settings = get_settings()
    _require_secret(settings)
    user = authenticate_local(session, body.email, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return TokenResponse(token=issue_token(settings, user.id, user.token_version))


@router.post("/auth/plex/start", response_model=PlexStartResponse)
def plex_start(
    provider: Annotated[AuthProvider, Depends(get_plex_auth_provider)],
) -> PlexStartResponse:
    _require_secret(get_settings())
    challenge = provider.start()
    return PlexStartResponse(challenge_id=challenge.challenge_id, auth_url=challenge.auth_url)


@router.post("/auth/plex/poll", response_model=PlexPollResponse)
def plex_poll(
    body: PlexPollRequest,
    session: Annotated[Session, Depends(get_session)],
    provider: Annotated[AuthProvider, Depends(get_plex_auth_provider)],
) -> PlexPollResponse:
    settings = get_settings()
    _require_secret(settings)
    result = provider.poll(body.challenge_id)
    if result.status is not AuthStatus.authorized or result.identity is None:
        return PlexPollResponse(status=result.status.value)
    try:
        user = provision_from_identity(session, settings, result.identity)
    except AuthorizationError as exc:
        session.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    session.commit()
    return PlexPollResponse(
        status="authorized", token=issue_token(settings, user.id, user.token_version)
    )


@router.get("/me", response_model=MeResponse)
def me(
    session: Annotated[Session, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> MeResponse:
    settings = get_settings()
    user = resolve_user(session, settings, authorization)
    return MeResponse(
        needs_setup=is_first_user(session),
        registration_open=settings.registration_open,
        authenticated=user is not None,
        user=_user_response(user) if user is not None else None,
    )


@router.post("/auth/logout", status_code=204)
def logout(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Revoke *every* outstanding session for the caller by bumping their token version (review I3).
    A stateless token can't be individually torn up, so logout invalidates all of them at once —
    documented in docs/auth.md."""
    user.token_version += 1
    session.commit()


@router.post("/auth/password", response_model=TokenResponse)
def change_password(
    body: ChangePasswordRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> TokenResponse:
    """Change the caller's local password. Requires the current password; revokes other sessions and
    returns a fresh token so the caller stays logged in on this device (review I5)."""
    settings = get_settings()
    _require_secret(settings)
    if user.email is None or authenticate_local(session, user.email, body.current_password) is None:
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    try:
        change_local_password(session, user, body.new_password)
    except RegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return TokenResponse(token=issue_token(settings, user.id, user.token_version))


@router.post(
    "/auth/admin/users/{user_id}/reset-password", response_model=AdminResetPasswordResponse
)
def admin_reset_password(
    user_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    admin: Annotated[User, Depends(get_current_user)],
) -> AdminResetPasswordResponse:
    """Admin-only: set a temporary password on another user and revoke their sessions (review I5).
    Returns the temporary password for the admin to hand over."""
    if not admin.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        temporary = reset_local_password(session, target)
    except RegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return AdminResetPasswordResponse(user_id=target.id, temporary_password=temporary)
