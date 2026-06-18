"""Poster URL composition: the engine carries a raw TMDB poster_path; the API turns it into a
full URL so the frontend stays dumb."""

from __future__ import annotations

import uuid

from phare.api.recommend import _poster_url, to_item
from phare.recommend.schema import Recommendation


def _rec(poster_path: str | None) -> Recommendation:
    return Recommendation(
        title_id=uuid.uuid4(),
        title="Blade Runner 2049",
        kind="movie",
        year=2017,
        genres=["Science Fiction"],
        score=1.0,
        poster_path=poster_path,
    )


def test_poster_url_appends_path_to_base() -> None:
    url = _poster_url("/abc.jpg")
    assert url is not None and url.endswith("/abc.jpg")
    assert url.startswith("https://image.tmdb.org/t/p/")


def test_poster_url_none_without_path() -> None:
    assert _poster_url(None) is None
    assert _poster_url("") is None


def test_to_item_exposes_poster_url() -> None:
    assert to_item(_rec("/p.jpg")).poster_url == "https://image.tmdb.org/t/p/w342/p.jpg"
    assert to_item(_rec(None)).poster_url is None
