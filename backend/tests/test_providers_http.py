"""HTTP-layer tests for the TMDB and Trakt clients using httpx MockTransport."""

from __future__ import annotations

import json

import httpx

from phare.db.models import TitleKind
from phare.providers.jellyfin import JellyfinSourceProvider, parse_jellyfin_item
from phare.providers.plex import PlexSourceProvider, parse_plex_item
from phare.providers.tmdb import TMDBMetadataProvider
from phare.providers.trakt import TraktSourceProvider

_MOVIE = {
    "id": 438631,
    "title": "Dune",
    "release_date": "2021-09-15",
    "runtime": 155,
    "overview": "Paul on Arrakis.",
    "genres": [{"id": 1, "name": "Science Fiction"}],
    "popularity": 123.4,
    "imdb_id": "tt1160419",
    "keywords": {"keywords": [{"id": 1, "name": "desert"}]},
}


def _tmdb_client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://tmdb.test")


def test_tmdb_get_movie() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/movie/438631"
        return httpx.Response(200, json=_MOVIE)

    provider = TMDBMetadataProvider(api_key="k", client=_tmdb_client(handler))
    meta = provider.get_title(438631, TitleKind.movie)

    assert meta is not None
    assert meta.title == "Dune"
    assert meta.year == 2021
    assert meta.runtime_minutes == 155
    assert meta.genres == ["Science Fiction"]
    assert meta.keywords == ["desert"]
    assert meta.imdb_id == "tt1160419"


def test_tmdb_find_by_imdb() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/find/tt1160419"
        return httpx.Response(200, json={"movie_results": [{"id": 438631}], "tv_results": []})

    provider = TMDBMetadataProvider(api_key="k", client=_tmdb_client(handler))
    match = provider.find_by_imdb("tt1160419")

    assert match is not None
    assert match.tmdb_id == 438631
    assert match.kind is TitleKind.movie


def test_tmdb_popular_resolves_each_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/movie/popular":
            assert request.url.params.get("page") == "2"
            return httpx.Response(200, json={"results": [{"id": 438631}]})
        if request.url.path == "/movie/438631":
            return httpx.Response(200, json=_MOVIE)
        raise AssertionError(f"unexpected path {request.url.path}")

    provider = TMDBMetadataProvider(api_key="k", client=_tmdb_client(handler))
    metas = provider.popular(TitleKind.movie, page=2)

    assert len(metas) == 1
    assert metas[0].title == "Dune"  # fully resolved, not the thin popular record
    assert metas[0].keywords == ["desert"]


def test_trakt_retries_on_rate_limit() -> None:
    calls = {"history": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sync/history":
            calls["history"] += 1
            if calls["history"] == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "watched_at": "2024-01-02T20:00:00.000Z",
                        "type": "movie",
                        "movie": {"ids": {"trakt": 9, "tmdb": 438631}},
                    }
                ],
                headers={"X-Pagination-Page-Count": "1"},
            )
        return httpx.Response(200, json=[])  # empty ratings/watchlist

    slept: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://trakt.test")
    provider = TraktSourceProvider(
        client_id="c", access_token="t", client=client, sleep=slept.append
    )

    events = list(provider.pull())
    assert calls["history"] == 2  # backed off then retried the same page
    assert slept == [2.0]  # honoured Retry-After
    assert len(events) == 1


def test_trakt_gives_up_after_max_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://trakt.test")
    provider = TraktSourceProvider(
        client_id="c", access_token="t", client=client, max_retries=2, sleep=lambda _s: None
    )
    try:
        list(provider.pull())
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 429
    else:  # pragma: no cover
        raise AssertionError("expected the 429 to surface after retries are exhausted")


def test_trakt_pull_paginates_history() -> None:
    pages = {
        "1": [
            {
                "id": 1,
                "watched_at": "2024-01-02T20:00:00.000Z",
                "type": "movie",
                "movie": {"ids": {"trakt": 9, "tmdb": 438631}},
            }
        ],
        "2": [
            {
                "id": 2,
                "watched_at": "2024-03-04T10:00:00.000Z",
                "type": "episode",
                "episode": {"season": 1, "number": 2, "ids": {"trakt": 50}},
                "show": {"ids": {"trakt": 7, "tmdb": 95396}},
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sync/history":
            page = request.url.params.get("page", "1")
            return httpx.Response(
                200,
                content=json.dumps(pages[page]),
                headers={"Content-Type": "application/json", "X-Pagination-Page-Count": "2"},
            )
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://trakt.test")
    provider = TraktSourceProvider(client_id="c", access_token="t", client=client)

    events = list(provider.pull())
    assert len(events) == 2
    assert events[0].tmdb_id == 438631
    assert events[1].season_number == 1
    assert events[1].episode_number == 2


# --- Plex ------------------------------------------------------------------

_PLEX_HISTORY = {
    "MediaContainer": {
        "Metadata": [
            {
                "type": "movie",
                "historyKey": "/status/sessions/history/1",
                "viewedAt": 1700000000,
                "Guid": [{"id": "tmdb://438631"}, {"id": "imdb://tt1160419"}],
            },
            {
                "type": "episode",
                "historyKey": "/status/sessions/history/2",
                "viewedAt": 1700100000,
                "parentIndex": 1,
                "index": 2,
                "grandparentGuids": [{"id": "tmdb://95396"}],
            },
        ]
    }
}


def test_plex_pull_maps_movie_and_episode() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status/sessions/history/all"
        assert request.url.params.get("accountID") == "7"
        return httpx.Response(200, json=_PLEX_HISTORY)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://plex.test")
    provider = PlexSourceProvider(
        base_url="https://plex.test", token="t", account_id="7", client=client
    )

    events = list(provider.pull())
    assert len(events) == 2
    assert events[0].tmdb_id == 438631
    assert events[0].imdb_id == "tt1160419"
    assert events[1].tmdb_id == 95396
    assert (events[1].season_number, events[1].episode_number) == (1, 2)


def test_plex_parse_skips_unsupported_type() -> None:
    assert parse_plex_item({"type": "track", "ratingKey": "9"}) is None


# --- Jellyfin --------------------------------------------------------------

_JELLYFIN_ITEMS = {
    "Items": [
        {
            "Type": "Movie",
            "Id": "m1",
            "ProviderIds": {"Tmdb": "438631", "Imdb": "tt1160419"},
            "UserData": {"LastPlayedDate": "2024-11-02T20:00:00.000Z"},
        },
        {
            "Type": "Episode",
            "Id": "e1",
            "ParentIndexNumber": 1,
            "IndexNumber": 2,
            "SeriesProviderIds": {"Tmdb": "95396"},
            "UserData": {"LastPlayedDate": "2025-01-11T19:00:00.000Z"},
        },
    ]
}


def test_jellyfin_pull_maps_movie_and_episode() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Users/u1/Items"
        assert request.url.params.get("Filters") == "IsPlayed"
        return httpx.Response(200, json=_JELLYFIN_ITEMS)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://jf.test")
    provider = JellyfinSourceProvider(
        base_url="https://jf.test", api_key="k", user_id="u1", client=client
    )

    events = list(provider.pull())
    assert len(events) == 2
    assert events[0].tmdb_id == 438631
    assert events[1].tmdb_id == 95396
    assert (events[1].season_number, events[1].episode_number) == (1, 2)


def test_jellyfin_parse_skips_unsupported_type() -> None:
    assert parse_jellyfin_item({"Type": "Audio", "Id": "x"}) is None
