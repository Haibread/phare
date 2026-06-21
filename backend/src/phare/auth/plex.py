"""Plex "Sign in with Plex" — PIN auth + server-membership signal.

The PIN flow: request a PIN from plex.tv, send the user to ``app.plex.tv/auth`` with its code, then
poll the PIN until it carries an ``authToken``. With the token we read the Plex account (the
identity) and the servers it can access (the membership signal the service uses to gate sign-in).
Parsing is split from HTTP so the mapping unit-tests without a live Plex. See ``docs/auth.md``.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from urllib.parse import quote

import httpx

from phare.auth.provider import AuthChallenge, AuthPollResult, AuthStatus, ResolvedIdentity
from phare.providers.http import request_with_retry

logger = logging.getLogger(__name__)

PLEX_API_BASE = "https://plex.tv/api/v2"
PLEX_AUTH_APP = "https://app.plex.tv/auth"


def derive_client_identifier(seed: str) -> str:
    """A stable client id derived from the instance secret, so plex.tv sees the same client."""
    return f"phare-{hashlib.sha256(seed.encode()).hexdigest()[:24]}"


def build_auth_url(client_id: str, code: str, product: str) -> str:
    """The app.plex.tv consent URL the frontend opens for the user."""
    return (
        f"{PLEX_AUTH_APP}#?clientID={quote(client_id)}"
        f"&code={quote(code)}"
        f"&context%5Bdevice%5D%5Bproduct%5D={quote(product)}"
    )


def parse_account(data: dict[str, Any]) -> tuple[str, str, str | None]:
    """(subject, display_name, email) from a plex.tv ``/user`` payload."""
    subject = str(data.get("uuid") or data.get("id") or "")
    display_name = str(data.get("title") or data.get("username") or "Plex user")
    email = data.get("email")
    return subject, display_name, (str(email) if email else None)


def parse_server_ids(resources: list[dict[str, Any]]) -> tuple[str, ...]:
    """Machine identifiers of the Plex *servers* an account can reach (owned or shared)."""
    ids: list[str] = []
    for resource in resources:
        provides = str(resource.get("provides", ""))
        machine_id = resource.get("clientIdentifier")
        if machine_id and "server" in provides.split(","):
            ids.append(str(machine_id))
    return tuple(ids)


class PlexAuthProvider:
    """Drives Plex PIN auth and reads the signing-in account's identity + server membership."""

    name = "plex"

    def __init__(
        self,
        client_identifier: str,
        product: str = "Phare",
        client: httpx.Client | None = None,
    ) -> None:
        self._client_id = client_identifier
        self._product = product
        self._client = client or httpx.Client(base_url=PLEX_API_BASE, timeout=15.0)

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Plex-Product": self._product,
            "X-Plex-Client-Identifier": self._client_id,
        }
        if token is not None:
            headers["X-Plex-Token"] = token
        return headers

    def start(self) -> AuthChallenge:
        """Request a PIN and build the consent URL the user opens."""
        response = request_with_retry(
            self._client,
            "POST",
            "/pins",
            name="plex_auth",
            params={"strong": "true"},
            headers=self._headers(),
        )
        response.raise_for_status()
        data = response.json()
        logger.info("plex_auth.pin_requested")
        return AuthChallenge(
            challenge_id=str(data["id"]),
            auth_url=build_auth_url(self._client_id, str(data["code"]), self._product),
        )

    def poll(self, challenge_id: str) -> AuthPollResult:
        """Poll the PIN once: pending until authorized, then resolve the identity."""
        response = request_with_retry(
            self._client,
            "GET",
            f"/pins/{challenge_id}",
            name="plex_auth",
            headers=self._headers(),
        )
        if response.status_code == 404:
            return AuthPollResult(status=AuthStatus.expired)
        response.raise_for_status()
        token = response.json().get("authToken")
        if not token:
            return AuthPollResult(status=AuthStatus.pending)
        return AuthPollResult(status=AuthStatus.authorized, identity=self._resolve(token))

    def _resolve(self, token: str) -> ResolvedIdentity:
        account = request_with_retry(
            self._client, "GET", "/user", name="plex_auth", headers=self._headers(token)
        )
        account.raise_for_status()
        subject, display_name, email = parse_account(account.json())

        resources = request_with_retry(
            self._client,
            "GET",
            "/resources",
            name="plex_auth",
            params={"includeHttps": "1"},
            headers=self._headers(token),
        )
        resources.raise_for_status()
        server_ids = parse_server_ids(resources.json())
        logger.info("plex_auth.resolved", extra={"servers": len(server_ids)})
        return ResolvedIdentity(
            provider="plex",
            subject=subject,
            display_name=display_name,
            email=email,
            access_token=token,
            server_ids=server_ids,
        )
