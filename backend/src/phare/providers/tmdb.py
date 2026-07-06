"""TMDB metadata provider.

Resolves canonical title metadata and maps IMDb ids to TMDB. The source of embedding
inputs (overview, genres, keywords) and the global popularity signal.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from phare.db.models import TitleKind
from phare.providers.http import DEFAULT_MAX_RETRIES, TTLCache, request_with_retry
from phare.providers.types import ExternalMatch, TitleMetadata

logger = logging.getLogger(__name__)

# TMDB metadata changes slowly, so idempotent reads are cached process-wide (shared across the
# per-request provider instances) to cut repeat third-party traffic — e.g. the chat agent resolving
# the same title on consecutive turns. The TTL is operator-tunable via TMDB_CACHE_TTL_SECONDS.
DEFAULT_CACHE_TTL_SECONDS = 3600
_shared_cache: TTLCache | None = None


def _get_shared_cache(ttl: float) -> TTLCache:
    """Process-wide TMDB read cache, created once. The first construction's TTL wins; since it
    comes from a single settings value that's the same every time, that's fine."""
    global _shared_cache
    if _shared_cache is None:
        _shared_cache = TTLCache(ttl=ttl, maxsize=4096)
    return _shared_cache


def _year(date_str: str | None) -> int | None:
    if date_str and len(date_str) >= 4 and date_str[:4].isdigit():
        return int(date_str[:4])
    return None


class TMDBMetadataProvider:
    """MetadataProvider backed by the TMDB v3 HTTP API."""

    name = "tmdb"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.themoviedb.org/3",
        client: httpx.Client | None = None,
        *,
        language: str | None = None,
        cache: TTLCache | None = None,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(base_url=base_url, timeout=10.0)
        # TMDB serves localised titles/overviews/genres when asked; None lets it use its default.
        # The language is part of the cache key (built from params), so languages cache separately.
        self._language = language
        # Tests inject an isolated cache; production shares one across request-scoped instances.
        self._cache = cache if cache is not None else _get_shared_cache(cache_ttl)
        self._max_retries = max_retries
        self._sleep = sleep

    def _fetch(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        logger.debug("tmdb.request", extra={"path": path})
        response = request_with_retry(
            self._client,
            "GET",
            path,
            name="tmdb",
            max_retries=self._max_retries,
            sleep=self._sleep,
            params={**params, "api_key": self._api_key},
        )
        response.raise_for_status()
        return response.json()

    def _get(self, path: str, **params: str) -> dict[str, Any]:
        return self._get_params(path, params)

    def _get_params(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        # Cache key excludes the api_key so it stays stable; reads are idempotent. Takes a dict so
        # callers can pass TMDB's dotted params (e.g. ``vote_count.gte``) that aren't kwarg names.
        if self._language is not None:
            params = {"language": self._language, **params}
        key = (path, tuple(sorted(params.items())))
        return self._cache.get_or_set(key, lambda: self._fetch(path, params))

    def get_title(self, tmdb_id: int, kind: TitleKind) -> TitleMetadata | None:
        # ``append_to_response`` bundles the sub-resources into the *same* HTTP request, so credits
        # cost no extra round-trip. Movie credits are a flat cast/crew list; TV has none at the
        # series level, so we append ``aggregate_credits`` (cross-season billing) for cast and take
        # the directors slot from the base payload's ``created_by`` (the show's creators) — no
        # separate call either way.
        if kind is TitleKind.movie:
            data = self._get(f"/movie/{tmdb_id}", append_to_response="keywords,credits")
            return self._parse_movie(data)
        data = self._get(
            f"/tv/{tmdb_id}", append_to_response="keywords,external_ids,aggregate_credits"
        )
        return self._parse_show(data)

    def _resolve_listing(self, kind: TitleKind, data: dict[str, Any]) -> list[TitleMetadata]:
        """Resolve a thin TMDB list response (popular / trending / now-playing) into full metadata.

        These list endpoints return shallow records, so we re-fetch each via ``get_title`` to get
        the depth (genres, keywords, runtime) the embedder relies on. Cached, so the per-title
        fan-out is cheap on repeat.
        """
        out: list[TitleMetadata] = []
        for result in data.get("results", []):
            tmdb_id = result.get("id")
            if tmdb_id is None:
                continue
            meta = self.get_title(tmdb_id, kind)
            if meta is not None:
                out.append(meta)
        return out

    def popular(self, kind: TitleKind, page: int = 1) -> list[TitleMetadata]:
        """List popular titles for catalog seeding. Each is fully resolved (genres/keywords)."""
        path = "/movie/popular" if kind is TitleKind.movie else "/tv/popular"
        return self._resolve_listing(kind, self._get(path, page=str(page)))

    def trending(self, kind: TitleKind, page: int = 1) -> list[TitleMetadata]:
        """This week's trending titles — catches new + buzzy content for the freshness refresh."""
        path = f"/trending/{'movie' if kind is TitleKind.movie else 'tv'}/week"
        return self._resolve_listing(kind, self._get(path, page=str(page)))

    def now_playing(self, kind: TitleKind, page: int = 1) -> list[TitleMetadata]:
        """Currently-released titles (movies in theatres / shows on the air) — the freshest content.

        Unlike ``discover`` there's no vote-count floor, so brand-new releases (which have barely
        any votes yet) actually show up. That's the whole point of the freshness pull.
        """
        path = "/movie/now_playing" if kind is TitleKind.movie else "/tv/on_the_air"
        return self._resolve_listing(kind, self._get(path, page=str(page)))

    def genres(self, kind: TitleKind) -> dict[int, str]:
        """TMDB's genre id -> name map for a kind (cached). Used both to resolve ``discover``
        results and to enumerate the genres a broad catalog seed sweeps over."""
        path = "/genre/movie/list" if kind is TitleKind.movie else "/genre/tv/list"
        data = self._get(path)
        return {g["id"]: g["name"] for g in data.get("genres", []) if g.get("id") and g.get("name")}

    def discover(
        self,
        kind: TitleKind,
        *,
        genre_id: int | None = None,
        min_vote_count: int = 0,
        sort_by: str = "vote_count.desc",
        page: int = 1,
    ) -> list[TitleMetadata]:
        """Page TMDB's discover endpoint for a broad catalog seed.

        Thin parse (genres resolved from ids, but no keywords/runtime — same depth as ``search``),
        so seeding tens of thousands of titles avoids the per-title ``get_title`` fan-out
        ``popular`` does. Sorting by vote count with a floor surfaces the well-known-but-not-
        blockbuster long tail rather than only the popular front page.
        """
        path = "/discover/movie" if kind is TitleKind.movie else "/discover/tv"
        params = {"page": str(page), "sort_by": sort_by, "include_adult": "false"}
        if min_vote_count > 0:
            params["vote_count.gte"] = str(min_vote_count)
        if genre_id is not None:
            params["with_genres"] = str(genre_id)
        data = self._get_params(path, params)
        genre_map = self.genres(kind)
        out: list[TitleMetadata] = []
        for result in data.get("results", []):
            meta = self._parse_discover(result, kind, genre_map)
            if meta is not None:
                out.append(meta)
        return out

    def search(self, query: str, *, limit: int = 8) -> list[TitleMetadata]:
        """Search movies + TV by title. Thin parse (no genres/runtime) for snappy on-demand
        search; full metadata is resolved later if the title gets recommended."""
        out: list[TitleMetadata] = []
        for kind, path in ((TitleKind.movie, "/search/movie"), (TitleKind.show, "/search/tv")):
            data = self._get(path, query=query)
            for result in data.get("results", [])[:limit]:
                meta = self._parse_search(result, kind)
                if meta is not None:
                    out.append(meta)
        out.sort(key=lambda m: m.popularity or 0.0, reverse=True)
        return out[:limit]

    @staticmethod
    def _parse_search(result: dict[str, Any], kind: TitleKind) -> TitleMetadata | None:
        tmdb_id = result.get("id")
        if tmdb_id is None:
            return None
        if kind is TitleKind.movie:
            title = result.get("title") or result.get("original_title") or ""
            year = _year(result.get("release_date"))
        else:
            title = result.get("name") or result.get("original_name") or ""
            year = _year(result.get("first_air_date"))
        if not title:
            return None
        return TitleMetadata(
            kind=kind,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
            overview=result.get("overview"),
            poster_path=result.get("poster_path"),
            popularity=result.get("popularity"),
            vote_count=result.get("vote_count"),
            vote_average=result.get("vote_average"),
        )

    @staticmethod
    def _parse_discover(
        result: dict[str, Any], kind: TitleKind, genre_map: dict[int, str]
    ) -> TitleMetadata | None:
        tmdb_id = result.get("id")
        if tmdb_id is None:
            return None
        if kind is TitleKind.movie:
            title = result.get("title") or result.get("original_title") or ""
            year = _year(result.get("release_date"))
        else:
            title = result.get("name") or result.get("original_name") or ""
            year = _year(result.get("first_air_date"))
        if not title:
            return None
        genres = [genre_map[g] for g in result.get("genre_ids", []) if g in genre_map]
        return TitleMetadata(
            kind=kind,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
            overview=result.get("overview"),
            poster_path=result.get("poster_path"),
            genres=genres,
            # discover carries original_language even in the shallow list parse — wire it through so
            # the broad seed populates it for free (no per-title fetch needed for language).
            original_language=result.get("original_language"),
            popularity=result.get("popularity"),
            vote_count=result.get("vote_count"),
            vote_average=result.get("vote_average"),
        )

    def find_by_imdb(self, imdb_id: str) -> ExternalMatch | None:
        data = self._get(f"/find/{imdb_id}", external_source="imdb_id")
        if movies := data.get("movie_results"):
            return ExternalMatch(tmdb_id=movies[0]["id"], kind=TitleKind.movie)
        if shows := data.get("tv_results"):
            return ExternalMatch(tmdb_id=shows[0]["id"], kind=TitleKind.show)
        return None

    @staticmethod
    def _genres(data: dict[str, Any]) -> list[str]:
        return [g["name"] for g in data.get("genres", []) if g.get("name")]

    # How many billed cast names to keep — the "top_cast" the detail sheet shows. Order is TMDB's
    # billing order, so the first N are the leads.
    _TOP_CAST = 5

    @staticmethod
    def _movie_directors(credits: dict[str, Any]) -> list[str]:
        """Director name(s) from a movie's ``credits.crew``, filtered to job=Director, in order."""
        return [
            name
            for member in credits.get("crew", [])
            if member.get("job") == "Director" and (name := member.get("name"))
        ]

    @staticmethod
    def _top_cast(credits: dict[str, Any]) -> list[str]:
        """First ``_TOP_CAST`` billed cast names from a ``credits``/``aggregate_credits`` block."""
        return [
            name
            for member in credits.get("cast", [])[: TMDBMetadataProvider._TOP_CAST]
            if (name := member.get("name"))
        ]

    @staticmethod
    def _parse_movie(data: dict[str, Any]) -> TitleMetadata:
        keywords = data.get("keywords", {}).get("keywords", [])
        credits = data.get("credits", {})
        return TitleMetadata(
            kind=TitleKind.movie,
            tmdb_id=data["id"],
            imdb_id=data.get("imdb_id"),
            title=data.get("title") or data.get("original_title", ""),
            year=_year(data.get("release_date")),
            runtime_minutes=data.get("runtime"),
            overview=data.get("overview"),
            poster_path=data.get("poster_path"),
            genres=TMDBMetadataProvider._genres(data),
            keywords=[k["name"] for k in keywords if k.get("name")],
            directors=TMDBMetadataProvider._movie_directors(credits),
            top_cast=TMDBMetadataProvider._top_cast(credits),
            original_language=data.get("original_language"),
            popularity=data.get("popularity"),
            vote_count=data.get("vote_count"),
            vote_average=data.get("vote_average"),
        )

    @staticmethod
    def _parse_show(data: dict[str, Any]) -> TitleMetadata:
        keywords = data.get("keywords", {}).get("results", [])
        runtimes = data.get("episode_run_time") or []
        # TV has no series-level crew, so the "directors" slot uses the show's creators
        # (``created_by``); cast comes from ``aggregate_credits`` (cross-season billing) — both in
        # the one appended fetch.
        creators = [c["name"] for c in data.get("created_by", []) if c.get("name")]
        return TitleMetadata(
            kind=TitleKind.show,
            tmdb_id=data["id"],
            imdb_id=data.get("external_ids", {}).get("imdb_id"),
            title=data.get("name") or data.get("original_name", ""),
            year=_year(data.get("first_air_date")),
            runtime_minutes=runtimes[0] if runtimes else None,
            overview=data.get("overview"),
            poster_path=data.get("poster_path"),
            genres=TMDBMetadataProvider._genres(data),
            keywords=[k["name"] for k in keywords if k.get("name")],
            directors=creators,
            top_cast=TMDBMetadataProvider._top_cast(data.get("aggregate_credits", {})),
            original_language=data.get("original_language"),
            popularity=data.get("popularity"),
            vote_count=data.get("vote_count"),
            vote_average=data.get("vote_average"),
        )
