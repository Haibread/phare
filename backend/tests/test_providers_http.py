"""HTTP-layer tests for the TMDB and Trakt clients using httpx MockTransport."""

from __future__ import annotations

import json

import httpx

from phare.db.models import TitleKind
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
