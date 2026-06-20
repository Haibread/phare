"""HTTP-layer tests for the TMDB and Trakt clients using httpx MockTransport."""

from __future__ import annotations

import json

import httpx

from phare.db.models import TitleKind
from phare.providers.http import TTLCache, request_with_retry, retry_after_seconds
from phare.providers.jellyfin import JellyfinSourceProvider, parse_jellyfin_item
from phare.providers.llm import OpenAILLMProvider
from phare.providers.plex import PlexSourceProvider, parse_plex_item
from phare.providers.seerr import SeerrProvider
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
    "poster_path": "/dune.jpg",
    "keywords": {"keywords": [{"id": 1, "name": "desert"}]},
}


def _tmdb_client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://tmdb.test")


def _tmdb(handler: object, **kwargs: object) -> TMDBMetadataProvider:
    # ttl=0 keeps these tests isolated from the process-wide cache so every call hits the handler;
    # the cache itself is exercised explicitly in test_tmdb_caches_reads.
    kwargs.setdefault("cache", TTLCache(ttl=0))
    return TMDBMetadataProvider(api_key="k", client=_tmdb_client(handler), **kwargs)  # type: ignore[arg-type]


def _llm_client(captured: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://llm.test")


def test_embed_sends_dimensions_only_when_configured() -> None:
    # Default: no `dimensions` (some models reject it).
    body: dict = {}
    OpenAILLMProvider("k", "chat", "embed", client=_llm_client(body)).embed(["x"])
    assert "dimensions" not in body

    # Opted in: the requested size is sent so a Matryoshka model matches the schema.
    body = {}
    OpenAILLMProvider(
        "k", "chat", "embed", client=_llm_client(body), embedding_dimensions=1536
    ).embed(["x"])
    assert body["dimensions"] == 1536


def test_tmdb_get_movie() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/movie/438631"
        return httpx.Response(200, json=_MOVIE)

    provider = _tmdb(handler)
    meta = provider.get_title(438631, TitleKind.movie)

    assert meta is not None
    assert meta.title == "Dune"
    assert meta.year == 2021
    assert meta.runtime_minutes == 155
    assert meta.genres == ["Science Fiction"]
    assert meta.keywords == ["desert"]
    assert meta.imdb_id == "tt1160419"
    assert meta.poster_path == "/dune.jpg"


def test_tmdb_search_movies_and_shows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/movie":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 1,
                            "title": "Blade Runner 2049",
                            "release_date": "2017-10-04",
                            "poster_path": "/br.jpg",
                            "popularity": 50.0,
                        }
                    ]
                },
            )
        if request.url.path == "/search/tv":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 2,
                            "name": "Severance",
                            "first_air_date": "2022-02-18",
                            "popularity": 80.0,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    provider = _tmdb(handler)
    results = provider.search("x")

    assert {m.title for m in results} == {"Blade Runner 2049", "Severance"}
    assert results[0].title == "Severance"  # sorted by popularity desc
    assert next(m for m in results if m.tmdb_id == 1).poster_path == "/br.jpg"
    assert next(m for m in results if m.tmdb_id == 1).year == 2017


def test_tmdb_find_by_imdb() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/find/tt1160419"
        return httpx.Response(200, json={"movie_results": [{"id": 438631}], "tv_results": []})

    provider = _tmdb(handler)
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

    provider = _tmdb(handler)
    metas = provider.popular(TitleKind.movie, page=2)

    assert len(metas) == 1
    assert metas[0].title == "Dune"  # fully resolved, not the thin popular record
    assert metas[0].keywords == ["desert"]


def test_tmdb_caches_reads() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_MOVIE)

    provider = _tmdb(handler, cache=TTLCache(ttl=300))
    first = provider.get_title(438631, TitleKind.movie)
    second = provider.get_title(438631, TitleKind.movie)

    assert calls["n"] == 1  # the second identical read is served from cache, not TMDB
    assert first is not None and second is not None and first.title == second.title == "Dune"


def test_tmdb_retries_on_rate_limit() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json=_MOVIE)

    slept: list[float] = []
    provider = _tmdb(handler, sleep=slept.append)
    meta = provider.get_title(438631, TitleKind.movie)

    assert calls["n"] == 2  # backed off then retried
    assert slept == [3.0]  # honoured Retry-After
    assert meta is not None and meta.title == "Dune"


def test_llm_retries_on_rate_limit() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})

    slept: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://llm.test")
    provider = OpenAILLMProvider("k", "chat", "embed", client=client, sleep=slept.append)

    assert provider.complete("ping") == "pong"
    assert calls["n"] == 2
    assert slept == [1.0]


def test_seerr_retries_on_rate_limit() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"mediaInfo": {"status": 5}})

    slept: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://seerr.test")
    provider = SeerrProvider("https://seerr.test", "k", client=client, sleep=slept.append)

    avail = provider.availability(438631, TitleKind.movie)
    assert calls["n"] == 2
    assert slept == [1.0]
    assert avail.value == "available"


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


# --- shared http helpers ----------------------------------------------------


def test_ttl_cache_serves_then_expires() -> None:
    clock = {"t": 0.0}
    cache = TTLCache(ttl=10, clock=lambda: clock["t"])
    calls = {"n": 0}

    def factory() -> str:
        calls["n"] += 1
        return f"v{calls['n']}"

    assert cache.get_or_set("k", factory) == "v1"
    assert cache.get_or_set("k", factory) == "v1"  # cached
    clock["t"] = 11.0  # past the TTL
    assert cache.get_or_set("k", factory) == "v2"  # recomputed
    assert calls["n"] == 2


def test_ttl_cache_disabled_when_ttl_non_positive() -> None:
    cache = TTLCache(ttl=0)
    calls = {"n": 0}

    def factory() -> int:
        calls["n"] += 1
        return calls["n"]

    assert cache.get_or_set("k", factory) == 1
    assert cache.get_or_set("k", factory) == 2  # never cached
    assert calls["n"] == 2


def test_ttl_cache_evicts_lru() -> None:
    cache = TTLCache(ttl=100, maxsize=2)
    cache.get_or_set("a", lambda: "a")
    cache.get_or_set("b", lambda: "b")
    cache.get_or_set("a", lambda: "ignored")  # touch a → most-recently-used
    cache.get_or_set("c", lambda: "c")  # evicts b (the LRU)

    # Check the survivor first: fetching the evicted "b" would itself evict another entry.
    assert cache.get_or_set("a", lambda: "ignored") == "a"  # a survived (was touched)
    assert cache.get_or_set("b", lambda: "b2") == "b2"  # b was evicted, recomputed


def test_retry_after_seconds_parses_and_clamps() -> None:
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "5"})) == 5.0
    assert retry_after_seconds(httpx.Response(429)) == 1.0  # default when header absent
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "junk"})) == 1.0
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "999"})) == 60.0  # cap


def test_request_with_retry_returns_last_response_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x.test")
    response = request_with_retry(
        client, "GET", "/", name="x", max_retries=2, sleep=lambda _s: None
    )
    assert response.status_code == 429  # exhausted retries → caller decides (no implicit raise)
