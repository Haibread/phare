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
    # TMDB ISO-639-1 origin code ("en", "ja") — what the origin-scoped genre filter reads ("anime"
    # = Animation + "ja", see recommend/genres). None = not yet healed / unknown.
    original_language: str | None = None
    overview: str | None
    poster_path: str | None = None
    similarity: float  # cosine similarity to the taste centroid, in [-1, 1]
    # Multi-facet retrieval (round 10). When a candidate came through the per-facet merge,
    # ``similarity`` holds its *facet-relative* placement mapped back onto the cosine range —
    # different regions of a real embedding space have different raw-cosine scales (a dense action
    # neighbourhood reads systematically higher than a sparse one), so raw cosines merged across
    # facets would let one mode sweep the pool-relative ranking. These two then carry the rest:
    # ``raw_similarity`` — the honest raw cosine (the confidence blend's absolute band and swing
    # novelty must read the true scale, never the normalised one) and ``facet`` — the index of the
    # facet whose query surfaced it best. Both None on every single-vector retrieval path, where
    # ``similarity`` is the raw cosine as it always was.
    raw_similarity: float | None = None
    facet: int | None = None


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
    # Known runtime (minutes), when the candidate carried one — cited by the chat composer's
    # per-item facts so the reply can only claim durations the data supports. None = unknown.
    runtime_minutes: int | None = None
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
