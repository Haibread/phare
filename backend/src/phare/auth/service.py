"""Provisioning: turn a verified identity (local or source) into a :class:`User`.

This is where the policy lives — the Plex server-membership gate, "first sign-in = owner/admin",
and account *linking* (a source identity with a known email attaches to the existing local user
rather than forking a second account). Signing in with a source also persists its access token
(encrypted) so the source is connected for ingestion in the same act. See ``docs/auth.md``.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.auth.provider import ResolvedIdentity
from phare.core.auth import hash_password, verify_password
from phare.core.config import Settings
from phare.core.tokens import store_source_token
from phare.db.models import Identity, PlexServerBinding, Profile, User

logger = logging.getLogger(__name__)


class AuthorizationError(Exception):
    """A verified identity that isn't allowed an account (e.g. not on the owner's Plex server)."""


class RegistrationError(Exception):
    """A local registration that can't proceed (e.g. the email is already taken)."""


def is_first_user(session: Session) -> bool:
    """True when no account exists yet — the first one becomes admin (and Plex owner)."""
    return (session.scalar(select(func.count()).select_from(User)) or 0) == 0


def _new_user(session: Session, *, email: str | None, display_name: str, is_admin: bool) -> User:
    """Create a User and its 1:1 Profile."""
    user = User(email=email, display_name=display_name, is_admin=is_admin)
    session.add(user)
    session.flush()
    session.add(Profile(user_id=user.id, display_name=display_name))
    session.flush()
    return user


def register_local(
    session: Session, email: str, password: str, display_name: str, *, is_admin: bool
) -> User:
    """Create a local (email + password) account. Raises if the email is taken."""
    if session.scalar(select(User).where(User.email == email)) is not None:
        raise RegistrationError("That email is already registered")
    user = _new_user(session, email=email, display_name=display_name, is_admin=is_admin)
    session.add(
        Identity(user_id=user.id, provider="local", subject=email, secret=hash_password(password))
    )
    session.flush()
    logger.info("auth.register_local", extra={"user_id": str(user.id), "admin": is_admin})
    return user


def authenticate_local(session: Session, email: str, password: str) -> User | None:
    """Return the user iff a local identity matches the email + password, else ``None``."""
    identity = session.scalar(
        select(Identity).where(Identity.provider == "local", Identity.subject == email)
    )
    if identity is None or identity.secret is None:
        return None
    if not verify_password(identity.secret, password):
        return None
    return identity.user


def _check_plex_membership(session: Session, identity: ResolvedIdentity) -> bool:
    """Apply the Plex membership gate. Returns True if this sign-in becomes the owner.

    First Plex sign-in binds the account's servers (becomes the owner). Later sign-ins must share
    at least one bound server, else :class:`AuthorizationError`.
    """
    bindings = session.scalars(select(PlexServerBinding.machine_identifier)).all()
    if not bindings:
        if not identity.server_ids:
            raise AuthorizationError("This Plex account can't reach any Plex server")
        for machine_id in identity.server_ids:
            session.add(PlexServerBinding(machine_identifier=machine_id))
        session.flush()
        logger.info("auth.plex_owner_bound", extra={"servers": len(identity.server_ids)})
        return True
    if not set(identity.server_ids) & set(bindings):
        raise AuthorizationError("Your Plex account isn't a member of this server")
    return False


def provision_from_identity(
    session: Session, settings: Settings, identity: ResolvedIdentity
) -> User:
    """Resolve a source-verified identity to a user, applying the provider's gate.

    Links to an existing user when the identity's email is already known; otherwise creates a new
    account. Persists the source access token so the source is connected for ingestion.
    """
    is_owner = identity.provider == "plex" and _check_plex_membership(session, identity)

    existing = session.scalar(
        select(Identity).where(
            Identity.provider == identity.provider, Identity.subject == identity.subject
        )
    )
    if existing is not None:
        user = existing.user
    else:
        user = None
        if identity.email is not None:
            user = session.scalar(select(User).where(User.email == identity.email))
        if user is None:
            user = _new_user(
                session,
                email=identity.email,
                display_name=identity.display_name,
                is_admin=is_first_user(session) or is_owner,
            )
        session.add(
            Identity(
                user_id=user.id, provider=identity.provider, subject=identity.subject, secret=None
            )
        )
        session.flush()

    store_source_token(session, settings, user.profile.id, identity.provider, identity.access_token)
    logger.info(
        "auth.provisioned",
        extra={"user_id": str(user.id), "provider": identity.provider},
    )
    return user
