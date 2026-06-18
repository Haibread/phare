"""Seerr action provider — request a title and read its library availability.

We use Seerr for the acquire hand-off (request a recommendation) and to tell whether a title is
already in the library, requested, or freshly requestable. Parsing is pure so it unit-tests
without HTTP.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from phare.db.models import TitleKind
from phare.providers.types import MediaAvailability

logger = logging.getLogger(__name__)

# Seerr MediaStatus: 1 UNKNOWN, 2 PENDING, 3 PROCESSING, 4 PARTIALLY_AVAILABLE, 5 AVAILABLE.
_AVAILABLE = {4, 5}
_QUEUED = {2, 3}


def availability_from_media_info(media_info: dict[str, Any] | None) -> MediaAvailability:
    """Map a Seerr ``mediaInfo`` blob to our availability enum (None → never requested)."""
    if not media_info:
        return MediaAvailability.requestable
    status = media_info.get("status")
    if status in _AVAILABLE:
        return MediaAvailability.available
    if status in _QUEUED:
        return MediaAvailability.queued
    return MediaAvailability.requestable


def _media_type(kind: TitleKind) -> str:
    return "movie" if kind is TitleKind.movie else "tv"


class SeerrProvider:
    """Talks to a Seerr instance with an API key."""

    name = "seerr"

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=15.0,
        )

    def availability(self, tmdb_id: int, kind: TitleKind) -> MediaAvailability:
        response = self._client.get(f"/api/v1/{_media_type(kind)}/{tmdb_id}")
        if response.status_code == 404:
            return MediaAvailability.requestable
        response.raise_for_status()
        return availability_from_media_info(response.json().get("mediaInfo"))

    def request_title(self, tmdb_id: int, kind: TitleKind) -> bool:
        body: dict[str, Any] = {"mediaType": _media_type(kind), "mediaId": tmdb_id}
        if kind is TitleKind.show:
            body["seasons"] = "all"  # request every season; Seerr de-dupes already-available ones
        response = self._client.post("/api/v1/request", json=body)
        response.raise_for_status()
        logger.info("seerr.requested", extra={"tmdb_id": tmdb_id, "kind": kind.value})
        return True
