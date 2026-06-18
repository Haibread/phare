"""TMDB metadata provider.

Resolves canonical title metadata and maps IMDb ids to TMDB. The source of embedding
inputs (overview, genres, keywords) and the global popularity signal.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from phare.db.models import TitleKind
from phare.providers.types import ExternalMatch, TitleMetadata

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(base_url=base_url, timeout=10.0)

    def _get(self, path: str, **params: str) -> dict[str, Any]:
        params["api_key"] = self._api_key
        logger.debug("tmdb.request", extra={"path": path})
        response = self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def get_title(self, tmdb_id: int, kind: TitleKind) -> TitleMetadata | None:
        if kind is TitleKind.movie:
            data = self._get(f"/movie/{tmdb_id}", append_to_response="keywords")
            return self._parse_movie(data)
        data = self._get(f"/tv/{tmdb_id}", append_to_response="keywords,external_ids")
        return self._parse_show(data)

    def popular(self, kind: TitleKind, page: int = 1) -> list[TitleMetadata]:
        """List popular titles for catalog seeding. Each is fully resolved (genres/keywords).

        The popular list endpoint returns thin records, so we resolve each via ``get_title``
        to get the same metadata depth (keywords, runtime) the embedder relies on.
        """
        path = "/movie/popular" if kind is TitleKind.movie else "/tv/popular"
        data = self._get(path, page=str(page))
        out: list[TitleMetadata] = []
        for result in data.get("results", []):
            tmdb_id = result.get("id")
            if tmdb_id is None:
                continue
            meta = self.get_title(tmdb_id, kind)
            if meta is not None:
                out.append(meta)
        return out

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

    @staticmethod
    def _parse_movie(data: dict[str, Any]) -> TitleMetadata:
        keywords = data.get("keywords", {}).get("keywords", [])
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
            popularity=data.get("popularity"),
        )

    @staticmethod
    def _parse_show(data: dict[str, Any]) -> TitleMetadata:
        keywords = data.get("keywords", {}).get("results", [])
        runtimes = data.get("episode_run_time") or []
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
            popularity=data.get("popularity"),
        )
