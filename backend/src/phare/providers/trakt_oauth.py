"""Trakt OAuth device flow — the right fit for a headless / self-hosted app.

No redirect URI: the backend asks Trakt for a device + user code, the user enters the short
user code at the verification URL, and the backend polls until Trakt hands back an access token.
Parsing is split from HTTP so the polling logic is unit-testable without a live Trakt.
"""

from __future__ import annotations

import enum
import logging

import httpx
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class DeviceCode(BaseModel):
    """The device/user code pair the user needs to authorize the app."""

    model_config = ConfigDict(frozen=True)

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


class PollStatus(enum.StrEnum):
    pending = "pending"  # user hasn't authorized yet — keep polling
    connected = "connected"  # got a token
    slow_down = "slow_down"  # polling too fast — back off
    expired = "expired"  # device code expired — restart
    denied = "denied"  # user rejected the request


class PollResult(BaseModel):
    """Outcome of one poll. ``access_token`` is set only when ``status`` is ``connected``."""

    model_config = ConfigDict(frozen=True)

    status: PollStatus
    access_token: str | None = None
    refresh_token: str | None = None


# Trakt's device-token endpoint signals state through HTTP status codes.
_STATUS_BY_CODE: dict[int, PollStatus] = {
    400: PollStatus.pending,
    409: PollStatus.pending,  # already approved, token issued elsewhere — treat as still pending
    429: PollStatus.slow_down,
    410: PollStatus.expired,
    418: PollStatus.denied,
}


class TraktOAuth:
    """Drives Trakt's OAuth device flow (requires the app's client id + secret)."""

    name = "trakt-oauth"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = "https://api.trakt.tv",
        client: httpx.Client | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = client or httpx.Client(base_url=base_url, timeout=15.0)

    def request_device_code(self) -> DeviceCode:
        """Start the flow: get the codes + verification URL to show the user."""
        response = self._client.post("/oauth/device/code", json={"client_id": self._client_id})
        response.raise_for_status()
        logger.info("trakt_oauth.device_code")
        return DeviceCode.model_validate(response.json())

    def poll_token(self, device_code: str) -> PollResult:
        """Poll once for a token. Never raises on the expected device-flow states."""
        response = self._client.post(
            "/oauth/device/token",
            json={
                "code": device_code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        if response.status_code == 200:
            data = response.json()
            return PollResult(
                status=PollStatus.connected,
                access_token=data.get("access_token"),
                refresh_token=data.get("refresh_token"),
            )
        status = _STATUS_BY_CODE.get(response.status_code)
        if status is None:
            response.raise_for_status()  # genuinely unexpected — surface it
        logger.debug("trakt_oauth.poll", extra={"status": status})
        return PollResult(status=status or PollStatus.pending)
