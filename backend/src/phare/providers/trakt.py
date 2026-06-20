"""Trakt source provider.

Pulls watched history, ratings, and watchlist for an authenticated user and maps them to
the normalized ``RawEvent`` stream. Parsing is split into pure functions so it can be unit
tested without HTTP.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import httpx

from phare.db.models import EventType
from phare.providers.http import request_with_retry
from phare.providers.types import RawEvent, RawMediaType

logger = logging.getLogger(__name__)


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def parse_history_item(item: dict[str, Any]) -> RawEvent | None:
    """Map a /sync/history item (a watch) to a RawEvent."""
    occurred_at = _dt(item.get("watched_at"))
    ref = f"history:{item.get('id')}"
    media = item.get("type")
    if media == "movie":
        ids = item["movie"]["ids"]
        return RawEvent(
            source="trakt",
            media_type=RawMediaType.movie,
            type=EventType.watched,
            tmdb_id=ids.get("tmdb"),
            imdb_id=ids.get("imdb"),
            occurred_at=occurred_at,
            external_ref=ref,
        )
    if media == "episode":
        show_ids = item["show"]["ids"]
        episode = item["episode"]
        return RawEvent(
            source="trakt",
            media_type=RawMediaType.episode,
            type=EventType.watched,
            tmdb_id=show_ids.get("tmdb"),
            imdb_id=show_ids.get("imdb"),
            season_number=episode.get("season"),
            episode_number=episode.get("number"),
            occurred_at=occurred_at,
            external_ref=ref,
        )
    return None


def parse_rating_item(item: dict[str, Any]) -> RawEvent | None:
    """Map a /sync/ratings item to a RawEvent."""
    occurred_at = _dt(item.get("rated_at"))
    rating = item.get("rating")
    media = item.get("type")
    if media == "movie":
        ids = item["movie"]["ids"]
        return RawEvent(
            source="trakt",
            media_type=RawMediaType.movie,
            type=EventType.rated,
            tmdb_id=ids.get("tmdb"),
            imdb_id=ids.get("imdb"),
            rating=rating,
            occurred_at=occurred_at,
            external_ref=f"rating:movie:{ids.get('trakt')}",
        )
    if media == "show":
        ids = item["show"]["ids"]
        return RawEvent(
            source="trakt",
            media_type=RawMediaType.show,
            type=EventType.rated,
            tmdb_id=ids.get("tmdb"),
            imdb_id=ids.get("imdb"),
            rating=rating,
            occurred_at=occurred_at,
            external_ref=f"rating:show:{ids.get('trakt')}",
        )
    if media == "episode":
        show_ids = item["show"]["ids"]
        episode = item["episode"]
        return RawEvent(
            source="trakt",
            media_type=RawMediaType.episode,
            type=EventType.rated,
            tmdb_id=show_ids.get("tmdb"),
            imdb_id=show_ids.get("imdb"),
            season_number=episode.get("season"),
            episode_number=episode.get("number"),
            rating=rating,
            occurred_at=occurred_at,
            external_ref=f"rating:episode:{episode['ids'].get('trakt')}",
        )
    return None


def parse_watchlist_item(item: dict[str, Any]) -> RawEvent | None:
    """Map a /sync/watchlist item to a RawEvent."""
    occurred_at = _dt(item.get("listed_at"))
    media = item.get("type")
    media_map = {"movie": RawMediaType.movie, "show": RawMediaType.show}
    if media not in media_map:
        return None
    ids = item[media]["ids"]
    return RawEvent(
        source="trakt",
        media_type=media_map[media],
        type=EventType.watchlisted,
        tmdb_id=ids.get("tmdb"),
        imdb_id=ids.get("imdb"),
        occurred_at=occurred_at,
        external_ref=f"watchlist:{media}:{ids.get('trakt')}",
    )


class TraktSourceProvider:
    """SourceProvider backed by the Trakt HTTP API (requires a user access token)."""

    name = "trakt"

    def __init__(
        self,
        client_id: str,
        access_token: str,
        base_url: str = "https://api.trakt.tv",
        client: httpx.Client | None = None,
        *,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": client_id,
            "Authorization": f"Bearer {access_token}",
        }
        self._client = client or httpx.Client(base_url=base_url, headers=headers, timeout=15.0)
        self._max_retries = max_retries
        self._sleep = sleep

    def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        """GET with bounded backoff on Trakt's 429 rate limit (honours Retry-After)."""
        response = request_with_retry(
            self._client,
            "GET",
            path,
            name="trakt",
            max_retries=self._max_retries,
            sleep=self._sleep,
            params=params,
        )
        response.raise_for_status()
        return response

    def _paginate(self, path: str, params: dict[str, str]) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            logger.debug("trakt.request", extra={"path": path, "page": page})
            response = self._get(path, params={**params, "page": page, "limit": 100})
            items = response.json()
            if not items:
                break
            yield from items
            page_count = int(response.headers.get("X-Pagination-Page-Count", page))
            if page >= page_count:
                break
            page += 1

    def pull(self, since: datetime | None = None) -> Iterator[RawEvent]:
        history_params: dict[str, str] = {}
        if since is not None:
            history_params["start_at"] = since.isoformat()

        for item in self._paginate("/sync/history", history_params):
            if event := parse_history_item(item):
                yield event
        for item in self._paginate("/sync/ratings", {}):
            if event := parse_rating_item(item):
                yield event
        for item in self._paginate("/sync/watchlist", {}):
            if event := parse_watchlist_item(item):
                yield event
