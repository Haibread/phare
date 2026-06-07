"""Trakt response parsing (pure, no HTTP)."""

from __future__ import annotations

from phare.db.models import EventType
from phare.providers.trakt import (
    parse_history_item,
    parse_rating_item,
    parse_watchlist_item,
)
from phare.providers.types import RawMediaType


def test_parse_movie_watch() -> None:
    item = {
        "id": 1,
        "watched_at": "2024-01-02T20:00:00.000Z",
        "type": "movie",
        "movie": {"title": "Dune", "ids": {"trakt": 9, "imdb": "tt1160419", "tmdb": 438631}},
    }
    event = parse_history_item(item)
    assert event is not None
    assert event.media_type is RawMediaType.movie
    assert event.type is EventType.watched
    assert event.tmdb_id == 438631
    assert event.external_ref == "history:1"
    assert event.occurred_at is not None


def test_parse_episode_watch() -> None:
    item = {
        "id": 2,
        "watched_at": "2024-03-04T10:00:00.000Z",
        "type": "episode",
        "episode": {"season": 2, "number": 5, "ids": {"trakt": 50}},
        "show": {"title": "Severance", "ids": {"trakt": 7, "tmdb": 95396}},
    }
    event = parse_history_item(item)
    assert event is not None
    assert event.media_type is RawMediaType.episode
    assert event.tmdb_id == 95396
    assert event.season_number == 2
    assert event.episode_number == 5


def test_parse_show_rating() -> None:
    item = {
        "rated_at": "2024-05-06T00:00:00.000Z",
        "rating": 9,
        "type": "show",
        "show": {"ids": {"trakt": 7, "tmdb": 95396}},
    }
    event = parse_rating_item(item)
    assert event is not None
    assert event.type is EventType.rated
    assert event.rating == 9
    assert event.media_type is RawMediaType.show
    assert event.external_ref == "rating:show:7"


def test_parse_watchlist_movie() -> None:
    item = {
        "listed_at": "2024-06-07T00:00:00.000Z",
        "type": "movie",
        "movie": {"ids": {"trakt": 9, "tmdb": 438631}},
    }
    event = parse_watchlist_item(item)
    assert event is not None
    assert event.type is EventType.watchlisted
    assert event.external_ref == "watchlist:movie:9"


def test_unknown_type_is_skipped() -> None:
    assert parse_history_item({"id": 3, "type": "person"}) is None
