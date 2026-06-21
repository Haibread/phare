"""The ``AuthProvider`` interface and its value types.

A provider drives a two-step challenge (``start`` → ``poll``), mirroring the device/PIN flows used
by Plex and Trakt: the backend starts a challenge, shows the user a URL/code, and polls until the
provider hands back an identity. Keeping it a Protocol means the engine and endpoints depend only
on the shape, so a fake stands in for tests (no live Plex). See ``docs/auth.md``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class AuthChallenge:
    """What the frontend needs to send the user to the provider's consent screen."""

    challenge_id: str  # opaque id the client passes back to ``poll``
    auth_url: str  # where to send the user (a popup/redirect target)


@dataclass(frozen=True)
class ResolvedIdentity:
    """A provider-verified identity, plus what we need to provision and connect the source.

    ``subject`` is the provider's stable id for this account (the identity key, with ``provider``).
    ``access_token`` is the source credential we persist (encrypted) so signing in also connects
    the source for ingestion. ``server_ids`` are the membership signal (Plex servers the account
    can access); empty for providers without a server concept.
    """

    provider: str
    subject: str
    display_name: str
    access_token: str
    email: str | None = None
    server_ids: tuple[str, ...] = field(default_factory=tuple)


class AuthStatus(enum.StrEnum):
    pending = "pending"  # user hasn't finished consent — keep polling
    authorized = "authorized"  # got an identity
    expired = "expired"  # the challenge timed out — restart


@dataclass(frozen=True)
class AuthPollResult:
    """Outcome of one poll. ``identity`` is set only when ``status`` is ``authorized``."""

    status: AuthStatus
    identity: ResolvedIdentity | None = None


class AuthProvider(Protocol):
    """A "Sign in with <source>" identity provider."""

    name: str

    def start(self) -> AuthChallenge:
        """Begin a challenge: returns the id to poll and the URL to send the user to."""
        ...

    def poll(self, challenge_id: str) -> AuthPollResult:
        """Poll once. Never raises on the expected pending/expired states."""
