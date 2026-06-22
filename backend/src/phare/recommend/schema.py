"""Value objects for the recommendation pipeline (internal; mapped to API DTOs at the edge)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class Candidate(BaseModel):
    """A catalog title scored by raw vector similarity, before re-ranking."""

    model_config = ConfigDict(frozen=True)

    title_id: uuid.UUID
    title: str
    kind: str
    year: int | None
    genres: list[str]
    keywords: list[str]
    runtime_minutes: int | None
    popularity: float | None
    vote_count: int | None = None  # TMDB rating count — proxy for how well-known a title is
    overview: str | None
    poster_path: str | None = None
    similarity: float  # cosine similarity to the taste centroid, in [-1, 1]


class Recommendation(BaseModel):
    """A re-ranked, optionally-explained recommendation. The unit the rows are built from."""

    title_id: uuid.UUID
    title: str
    kind: str
    year: int | None
    genres: list[str]
    score: float
    is_swing: bool = False
    confidence: float | None = None
    explanation: str | None = None
    poster_path: str | None = None
    # Transparent score breakdown — the engine is never a black box (principle 2).
    components: dict[str, float] = {}


class Row(BaseModel):
    """A titled strip of recommendations (``you_might_like``, ``watch_again``, ...)."""

    key: str
    title: str
    items: list[Recommendation]
