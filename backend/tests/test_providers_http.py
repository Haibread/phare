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
    "original_language": "en",
    "keywords": {"keywords": [{"id": 1, "name": "desert"}]},
    "credits": {
        "crew": [
            {"job": "Director", "name": "Denis Villeneuve"},
            {"job": "Editor", "name": "Joe Walker"},  # non-director crew ignored
            {"job": "Director", "name": "Co Director"},  # a second director kept, in order
        ],
        "cast": [
            {"name": "Timothée Chalamet"},
            {"name": "Rebecca Ferguson"},
            {"name": "Oscar Isaac"},
            {"name": "Josh Brolin"},
            {"name": "Stellan Skarsgård"},
            {"name": "Zendaya"},  # 6th billed — beyond the top-5 cap, dropped
        ],
    },
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
        # Credits are appended to the same request — no extra round-trip.
        assert "credits" in request.url.params["append_to_response"]
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
    assert meta.original_language == "en"
    # Only job=Director crew, in payload order; top_cast is the first 5 billed (6th dropped).
    assert meta.directors == ["Denis Villeneuve", "Co Director"]
    assert meta.top_cast == [
        "Timothée Chalamet",
        "Rebecca Ferguson",
        "Oscar Isaac",
        "Josh Brolin",
        "Stellan Skarsgård",
    ]


def test_tmdb_get_show_uses_created_by_and_aggregate_credits() -> None:
    show = {
        "id": 1399,
        "name": "Game of Thrones",
        "first_air_date": "2011-04-17",
        "episode_run_time": [60],
        # ``episode_run_time`` wins over the last-episode fallback when both are present.
        "last_episode_to_air": {"runtime": 80},
        "overview": "Westeros.",
        "genres": [{"id": 1, "name": "Sci-Fi & Fantasy"}],
        "original_language": "en",
        "keywords": {"results": [{"id": 1, "name": "dragons"}]},
        "external_ids": {"imdb_id": "tt0944947"},
        # TV has no series-level crew — the "directors" slot takes the show's creators, and cast
        # comes from aggregate_credits (both appended into the one fetch).
        "created_by": [{"name": "David Benioff"}, {"name": "D. B. Weiss"}],
        "aggregate_credits": {
            "cast": [{"name": f"Actor {i}"} for i in range(7)],  # 7 → capped at 5
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tv/1399"
        append = request.url.params["append_to_response"]
        assert "aggregate_credits" in append  # cast bundled, no separate call
        return httpx.Response(200, json=show)

    meta = _tmdb(handler).get_title(1399, TitleKind.show)

    assert meta is not None
    assert meta.runtime_minutes == 60  # episode_run_time first — not the last-episode 80
    assert meta.original_language == "en"
    assert meta.directors == ["David Benioff", "D. B. Weiss"]  # creators fill the directors slot
    assert meta.top_cast == ["Actor 0", "Actor 1", "Actor 2", "Actor 3", "Actor 4"]


def _show_payload(**extra: object) -> dict[str, object]:
    """A minimal TV detail payload; ``extra`` overlays the runtime-bearing fields under test."""
    return {"id": 1399, "name": "Some Show", "first_air_date": "2011-04-17", **extra}


def test_tmdb_show_episode_runtime_falls_back_to_last_episode_to_air() -> None:
    # Modern shows very often ship an EMPTY ``episode_run_time`` list, but the appended
    # ``last_episode_to_air`` block carries the latest episode's runtime — for a show,
    # ``runtime_minutes`` means EPISODE length, so that fallback is what keeps TV rows
    # filterable by a chat runtime cap.
    payload = _show_payload(episode_run_time=[], last_episode_to_air={"runtime": 52})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    meta = _tmdb(handler).get_title(1399, TitleKind.show)

    assert meta is not None
    assert meta.runtime_minutes == 52


def test_tmdb_show_episode_runtime_none_when_neither_source_has_it() -> None:
    # Neither ``episode_run_time`` nor a last-episode block at all → honest None, never a guess.
    payload = _show_payload(episode_run_time=[])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    meta = _tmdb(handler).get_title(1399, TitleKind.show)

    assert meta is not None
    assert meta.runtime_minutes is None


def test_tmdb_language_is_sent_and_localises_the_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["language"] == "fr"
        return httpx.Response(
            200,
            json={
                "id": 438631,
                "title": "Dune",
                "overview": "Paul sur Arrakis.",
                "genres": [{"id": 878, "name": "Science-Fiction"}],
            },
        )

    meta = _tmdb(handler, language="fr").get_title(438631, TitleKind.movie)

    assert meta is not None
    assert meta.overview == "Paul sur Arrakis."
    assert meta.genres == ["Science-Fiction"]


def test_tmdb_canonical_twin_drops_the_language_and_shares_the_cache() -> None:
    # canonical() is the language-neutral view for canonical Title writes: same client + cache, no
    # ``language`` param (TMDB's en-US default). The language sits in the cache key, so localized
    # and canonical reads cache separately — a canonical fetch never serves a localized payload.
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("language"))
        localized = request.url.params.get("language") == "fr"
        return httpx.Response(
            200,
            json={
                "id": 438631,
                "title": "Dune (fr)" if localized else "Dune",
                "overview": "Paul sur Arrakis." if localized else "Paul on Arrakis.",
            },
        )

    localized = _tmdb(handler, language="fr", cache=TTLCache(ttl=300))
    canonical = localized.canonical()

    loc_meta = localized.get_title(438631, TitleKind.movie)
    canon_meta = canonical.get_title(438631, TitleKind.movie)

    assert calls == ["fr", None]  # distinct cache keys → both fetched, canonical without language
    assert loc_meta is not None and loc_meta.overview == "Paul sur Arrakis."
    assert canon_meta is not None and canon_meta.overview == "Paul on Arrakis."
    # A repeat canonical read is a cache hit on the shared cache (no third HTTP call).
    canonical.get_title(438631, TitleKind.movie)
    assert len(calls) == 2
    # An already-neutral provider is its own canonical view — no twin churn.
    neutral = _tmdb(handler)
    assert neutral.canonical() is neutral


def test_tmdb_discover_resolves_genres_and_passes_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/genre/movie/list":
            return httpx.Response(200, json={"genres": [{"id": 28, "name": "Action"}]})
        assert request.url.path == "/discover/movie"
        params = request.url.params
        assert params["with_genres"] == "28"
        assert params["vote_count.gte"] == "100"
        assert params["sort_by"] == "vote_count.desc"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 13183,
                        "title": "Equilibrium",
                        "release_date": "2002-12-06",
                        "overview": "Emotion is a crime.",
                        "genre_ids": [28, 999],  # 999 is unknown -> dropped
                        "popularity": 21.0,
                        "original_language": "en",
                    }
                ]
            },
        )

    metas = _tmdb(handler).discover(TitleKind.movie, genre_id=28, min_vote_count=100)

    assert len(metas) == 1
    assert metas[0].title == "Equilibrium"
    assert metas[0].year == 2002
    assert metas[0].genres == ["Action"]  # resolved id->name, unknown id ignored
    assert metas[0].original_language == "en"  # discover carries language even in the thin parse


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
