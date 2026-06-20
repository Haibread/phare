"""Plex source provider — the account owner's own watch history.

Reads ``/status/sessions/history/all`` (optionally scoped to one ``accountID`` so it never pulls
a co-watcher's data). Maps Plex Metadata to the normalized ``RawEvent`` stream. Parsing is pure
so it unit-tests without HTTP.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from phare.db.models import EventType
from phare.providers.http import DEFAULT_MAX_RETRIES, request_with_retry
from phare.providers.types import RawEvent, RawMediaType

logger = logging.getLogger(__name__)


def _ids_from_guids(guids: list[dict[str, Any]]) -> tuple[int | None, str | None]:
    """Extract (tmdb_id, imdb_id) from a Plex Guid list, e.g. [{"id": "tmdb://1234"}]."""
    tmdb_id: int | None = None
    imdb_id: str | None = None
    for entry in guids:
        value = str(entry.get("id", ""))
        if value.startswith("tmdb://"):
            suffix = value.removeprefix("tmdb://")
            tmdb_id = int(suffix) if suffix.isdigit() else tmdb_id
        elif value.startswith("imdb://"):
            imdb_id = value.removeprefix("imdb://")
    return tmdb_id, imdb_id


def _occurred(item: dict[str, Any]) -> datetime | None:
    viewed = item.get("viewedAt")
    return datetime.fromtimestamp(int(viewed), UTC) if viewed else None


def parse_plex_item(item: dict[str, Any]) -> RawEvent | None:
    """Map a Plex history Metadata entry to a watched RawEvent (or None if unsupported)."""
    ref = f"plex:{item.get('historyKey') or item.get('ratingKey')}"
    occurred_at = _occurred(item)
    kind = item.get("type")
    if kind == "movie":
        tmdb_id, imdb_id = _ids_from_guids(item.get("Guid", []))
        return RawEvent(
            source="plex",
            media_type=RawMediaType.movie,
            type=EventType.watched,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            occurred_at=occurred_at,
            external_ref=ref,
        )
    if kind == "episode":
        # Show-level ids come from the grandparent (the series) when Plex provides them.
        tmdb_id, imdb_id = _ids_from_guids(item.get("grandparentGuids", []))
        return RawEvent(
            source="plex",
            media_type=RawMediaType.episode,
            type=EventType.watched,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            season_number=item.get("parentIndex"),
            episode_number=item.get("index"),
            occurred_at=occurred_at,
            external_ref=ref,
        )
    return None


class PlexSourceProvider:
    """SourceProvider over a Plex server's watch history for the token's owner."""

    name = "plex"

    def __init__(
        self,
        base_url: str,
        token: str,
        account_id: str | None = None,
        client: httpx.Client | None = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._account_id = account_id
        self._max_retries = max_retries
        self._sleep = sleep
        self._client = client or httpx.Client(
            base_url=base_url,
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=15.0,
        )

    def pull(self, since: datetime | None = None) -> Iterator[RawEvent]:
        params: dict[str, str] = {"sort": "viewedAt:desc"}
        if self._account_id is not None:
            params["accountID"] = self._account_id
        logger.debug("plex.request", extra={"account_id": self._account_id})
        response = request_with_retry(
            self._client,
            "GET",
            "/status/sessions/history/all",
            name="plex",
            max_retries=self._max_retries,
            sleep=self._sleep,
            params=params,
        )
        response.raise_for_status()
        container = response.json().get("MediaContainer", {})
        for item in container.get("Metadata", []):
            event = parse_plex_item(item)
            if event is None:
                continue
            if since is not None and event.occurred_at and event.occurred_at < since:
                continue
            yield event
