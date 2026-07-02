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
    vote_average: float | None = None  # TMDB mean rating [0, 10] — the re-ranker's quality floor
    overview: str | None
    poster_path: str | None = None
    similarity: float  # cosine similarity to the taste centroid, in [-1, 1]


class Anchor(BaseModel):
    """A loved title a recommendation is being shown *because of* — the seed of a "because you
    watched X" row. Carried into the explanation so the reason can open from that concrete link
    ("Because you loved Dune, ...") instead of only the abstract taste summary. Genres only, never
    the overview — the same spoiler rule the rest of the explain path follows."""

    model_config = ConfigDict(frozen=True)

    title_id: uuid.UUID
    title: str
    genres: list[str] = []


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
    # True when the profile has already watched this title — the UI badges it "Watched" so the same
    # title recurring in search / rewatch rows is honest about what you've seen (review A11).
    watched: bool = False
    # Internal-only (never mapped to the API DTO): the overview + keywords ground the LLM "why this"
    # prompt so it describes real appeal instead of inventing aligned qualities (review H4). The
    # template explanation never touches the overview, so it still can't leak plot.
    overview: str | None = None
    keywords: list[str] = []
    # Transparent score breakdown — the engine is never a black box (principle 2).
    components: dict[str, float] = {}


class Row(BaseModel):
    """A titled strip of recommendations (``you_might_like``, ``watch_again``, ...)."""

    key: str
    title: str
    items: list[Recommendation]
