"""Jellyfin source provider — the account owner's own played history.

Privacy-safe by construction: it reads a single user's ``IsPlayed`` items (no other users'
data). Maps Jellyfin items to the normalized ``RawEvent`` stream. Parsing is a pure function so
it unit-tests without HTTP.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import httpx

from phare.db.models import EventType
from phare.providers.http import DEFAULT_MAX_RETRIES, request_with_retry
from phare.providers.types import RawEvent, RawMediaType

logger = logging.getLogger(__name__)


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_jellyfin_item(item: dict[str, Any]) -> RawEvent | None:
    """Map a Jellyfin Movie/Episode item to a watched RawEvent (or None if unsupported)."""
    occurred_at = _dt(item.get("UserData", {}).get("LastPlayedDate"))
    ref = f"jellyfin:{item.get('Id')}"
    kind = item.get("Type")
    if kind == "Movie":
        provider = item.get("ProviderIds", {})
        return RawEvent(
            source="jellyfin",
            media_type=RawMediaType.movie,
            type=EventType.watched,
            tmdb_id=_int(provider.get("Tmdb")),
            imdb_id=provider.get("Imdb"),
            occurred_at=occurred_at,
            external_ref=ref,
        )
    if kind == "Episode":
        # Show-level ids live in SeriesProviderIds (requested via Fields); episode ids are useless
        # for our show-keyed model.
        series = item.get("SeriesProviderIds", {})
        return RawEvent(
            source="jellyfin",
            media_type=RawMediaType.episode,
            type=EventType.watched,
            tmdb_id=_int(series.get("Tmdb")),
            imdb_id=series.get("Imdb"),
            season_number=item.get("ParentIndexNumber"),
            episode_number=item.get("IndexNumber"),
            occurred_at=occurred_at,
            external_ref=ref,
        )
    return None


def list_jellyfin_users(
    base_url: str,
    api_key: str,
    client: httpx.Client | None = None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, str]]:
    """List a Jellyfin server's users as ``{"id", "name"}`` so the UI can offer a picker instead of
    asking the operator to paste a GUID (review D2). Read-only; the SSRF guard is enforced upstream
    at the endpoint via ``require_safe_url``."""
    owned = client or httpx.Client(
        base_url=base_url, headers={"X-Emby-Token": api_key}, timeout=15.0
    )
    try:
        response = request_with_retry(
            owned, "GET", "/Users", name="jellyfin", max_retries=max_retries, sleep=sleep
        )
        response.raise_for_status()
        return [
            {"id": str(user["Id"]), "name": str(user.get("Name") or user["Id"])}
            for user in response.json()
            if user.get("Id")
        ]
    finally:
        if client is None:
            owned.close()


class JellyfinSourceProvider:
    """SourceProvider over a Jellyfin server for one user (API key + user id)."""

    name = "jellyfin"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        user_id: str,
        client: httpx.Client | None = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._user_id = user_id
        self._max_retries = max_retries
        self._sleep = sleep
        self._client = client or httpx.Client(
            base_url=base_url, headers={"X-Emby-Token": api_key}, timeout=15.0
        )

    def pull(self, since: datetime | None = None) -> Iterator[RawEvent]:
        params = {
            "Recursive": "true",
            "Filters": "IsPlayed",
            "IncludeItemTypes": "Movie,Episode",
            "Fields": "ProviderIds,SeriesProviderIds,UserData",
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
        }
        logger.debug("jellyfin.request", extra={"user_id": self._user_id})
        response = request_with_retry(
            self._client,
            "GET",
            f"/Users/{self._user_id}/Items",
            name="jellyfin",
            max_retries=self._max_retries,
            sleep=self._sleep,
            params=params,
        )
        response.raise_for_status()
        for item in response.json().get("Items", []):
            event = parse_jellyfin_item(item)
            if event is None:
                continue
            if since is not None and event.occurred_at and event.occurred_at < since:
                continue
            yield event
