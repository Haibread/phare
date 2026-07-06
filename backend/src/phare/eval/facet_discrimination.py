"""Round-10 facet guardrail: prove per-facet retrieval beats a single averaged centroid.

The measured failure (rounds 5–9): a profile spanning two distinct taste modes averages into one
centroid that sits *between* them, near neither, so retrieval fetches generic mid-space titles and
the on-taste picks from each mode lose. Multi-facet retrieval clusters the taste into its modes and
queries each; the merged slate should carry items from *both* modes. The single centroid carries
neither.

This can't be shown on the offline hash embedder — its cosine geometry is degenerate (every pair of
distinct documents ≈0.85, and no partial-overlap signal at all: a one-word title change reads as
unrelated), so no two "modes" are ever separated enough for averaging to blur them. So the guardrail
uses a tiny **two-mode embedder** that places a title into one of two orthogonal subspaces by which
mode word its text carries — exactly the separation the *real* production embedder produces
semantically, made explicit and deterministic. On that space:

- the two modes' watched titles cluster cleanly into 2 facets (orthogonal → intra-sim ≈1, cross ≈0);
- each facet's ANN surfaces that mode's unwatched "cousins" at high similarity;
- the averaged centroid is the diagonal midpoint — cosine ≈0.7 to each mode, *below* the diffuse
  background catalog it then retrieves instead — so it surfaces neither mode's cousins.

The check: the facet slate contains ≥``_MIN_PER_MODE`` cousins from EACH mode; the single-centroid
slate fails that (it's the regression this whole round removes). Both are asserted in the test
(``test_facets_beat_single_centroid_on_mixed_taste``) so a check that passed both ways — proving
nothing — can't slip through.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from phare.db.models import EventType, Profile, Title, TitleKind, WatchEvent
from phare.embeddings.service import EmbeddingService
from phare.recommend.schema import Recommendation
from phare.recommend.service import RecommendationService
from phare.recommend.taste_facets import Facet
from phare.recommend.taste_vector import compute_taste_centroid

# The two taste modes. Real catalog genres so the affinity steering + the ≥N-per-mode assertion read
# naturally; the embedder keys off these exact words appearing in the embedding document.
MODE_A = "Horror"
MODE_B = "Comedy"
_MODEL_VERSION = "eval-twomode-v1"
_DIM = 1536  # must match the pgvector column width
_SEEDS_PER_MODE = 6  # enough that each mode forms a solid cluster
_COUSINS_PER_MODE = 4  # unwatched same-mode titles a good slate should surface
# The discrimination threshold: a passing (facet) slate carries at least this many cousins from
# EACH mode; the single-centroid slate carries fewer (it surfaces neither mode). Two is the smallest
# count that reads as "both modes are represented", not one lucky hit.
_MIN_PER_MODE = 2
MIXED_TASTE = {"affinities": {MODE_A: 0.9, MODE_B: 0.9}, "confidence": 0.7}


class TwoModeEmbedder:
    """A deterministic embedder that separates two taste modes into orthogonal subspaces.

    Mode A titles get a vector confined to the first half of the dimensions, mode B to the second
    half (so A·B = 0 — genuinely distinct modes). A small per-title hash perturbation keeps members
    of a mode non-identical, so the clusterer has real work to do. Everything else (the background
    sample catalog) gets a tiny diffuse vector across all dims — present, but far from either mode.
    Satisfies the ``embed`` half of ``LLMProvider``; never completes."""

    name = "eval-twomode"

    def complete(  # pragma: no cover - not a chat model
        self, prompt: str, *, max_tokens: int | None = None, temperature: float | None = None
    ) -> str:
        raise NotImplementedError("TwoModeEmbedder does not do chat completion")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _mode_vector(self, text: str, lo: int, hi: int) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        out = [0.0] * _DIM
        for j in range(lo, hi):
            out[j] = 0.2 + 0.1 * (digest[j % len(digest)] / 255.0)
        return out

    def _vector(self, text: str) -> list[float]:
        half = _DIM // 2
        if MODE_A in text:
            return self._mode_vector(text, 0, half)
        if MODE_B in text:
            return self._mode_vector(text, half, _DIM)
        digest = hashlib.sha256(text.encode()).digest()
        return [0.01 + 0.005 * (digest[j % len(digest)] / 255.0) for j in range(_DIM)]


@dataclass
class FacetDiscriminationResult:
    """Outcome of the facet vs single-centroid comparison on the mixed-taste persona."""

    facet_count: int
    facet_sizes: list[int]
    facet_a_cousins: int
    facet_b_cousins: int
    single_a_cousins: int
    single_b_cousins: int

    @property
    def facets_pass(self) -> bool:
        """Facets surface both modes — the goal."""
        return self.facet_a_cousins >= _MIN_PER_MODE and self.facet_b_cousins >= _MIN_PER_MODE

    @property
    def single_centroid_fails(self) -> bool:
        """The single averaged centroid misses a mode — the regression facets fix. If this is ever
        False the check proves nothing (it'd pass both ways) — the test asserts on it."""
        return self.single_a_cousins < _MIN_PER_MODE or self.single_b_cousins < _MIN_PER_MODE


def _add_title(
    session: Session, *, title: str, genres: list[str], keywords: list[str], tmdb_id: int
) -> Title:
    row = Title(
        kind=TitleKind.movie,
        tmdb_id=tmdb_id,
        title=title,
        year=2020,
        genres=genres,
        keywords=keywords,
        runtime_minutes=110,
        popularity=10.0,
        vote_count=5000,  # proven, so the reranker's unproven cap doesn't muddy the slate
        vote_average=7.5,
        overview="A mixed-taste eval fixture title.",
    )
    session.add(row)
    session.flush()
    return row


def _seed_mixed_taste(
    session: Session,
) -> tuple[uuid.UUID, set[uuid.UUID], set[uuid.UUID]]:
    """Seed a two-mode watch history plus unwatched same-mode cousins for each mode, on a fresh
    catalog. Returns ``(profile_id, mode_a_cousin_ids, mode_b_cousin_ids)``. The background sample
    catalog is intentionally *not* seeded here: the two-mode embedder maps it all to the diffuse
    subspace, which is exactly the mid-space filler the single centroid wrongly retrieves — the
    cousins are the on-taste picks the facet slate must find instead."""
    profile = Profile(display_name="eval-mixed-taste")
    session.add(profile)
    session.flush()

    watched: list[Title] = []
    for i in range(_SEEDS_PER_MODE):
        watched.append(
            _add_title(
                session,
                title=f"{MODE_A}Seed{i}",
                genres=[MODE_A],
                keywords=["dread", f"a{i}"],
                tmdb_id=990_000 + i,
            )
        )
    for i in range(_SEEDS_PER_MODE):
        watched.append(
            _add_title(
                session,
                title=f"{MODE_B}Seed{i}",
                genres=[MODE_B],
                keywords=["banter", f"b{i}"],
                tmdb_id=991_000 + i,
            )
        )
    for rank, title in enumerate(watched):
        session.add(
            WatchEvent(
                profile_id=profile.id,
                title_id=title.id,
                type=EventType.rated,
                rating=9.0,
                source="eval",
                external_ref=f"eval:mixed:{rank}",
            )
        )

    a_cousins = {
        _add_title(
            session,
            title=f"{MODE_A}Cousin{i}",
            genres=[MODE_A],
            keywords=["dread", f"ac{i}"],
            tmdb_id=992_000 + i,
        ).id
        for i in range(_COUSINS_PER_MODE)
    }
    b_cousins = {
        _add_title(
            session,
            title=f"{MODE_B}Cousin{i}",
            genres=[MODE_B],
            keywords=["banter", f"bc{i}"],
            tmdb_id=993_000 + i,
        ).id
        for i in range(_COUSINS_PER_MODE)
    }
    # Diffuse background filler: titles the single centroid wrongly prefers over the on-taste
    # cousins. Without them the tiny catalog would hand back the cousins to *any* query; they are
    # the mid-space noise the averaged centroid actually retrieves.
    for i in range(20):
        _add_title(
            session,
            title=f"Filler{i}",
            genres=["Drama"],
            keywords=["misc", f"f{i}"],
            tmdb_id=994_000 + i,
        )
    session.flush()
    return profile.id, a_cousins, b_cousins


def _cousin_counts(
    recs: list[Recommendation], a_cousins: set[uuid.UUID], b_cousins: set[uuid.UUID]
) -> tuple[int, int]:
    ids = {r.title_id for r in recs}
    return len(ids & a_cousins), len(ids & b_cousins)


def evaluate_facet_discrimination(session: Session, *, k: int = 12) -> FacetDiscriminationResult:
    """Run the mixed-taste persona twice — once with facets, once forced to a single centroid — and
    count each mode's cousins in each slate. The service is identical between runs; only the query
    structure differs, so the comparison isolates the facet split. Deterministic, no LLM."""
    profile_id, a_cousins, b_cousins = _seed_mixed_taste(session)
    embedder = TwoModeEmbedder()
    # Embed the WHOLE fixture catalog synchronously — not the read-path micro-batch (16 titles + a
    # background backfill), which would leave most cousins unembedded and make the pool composition
    # depend on embed order. The guardrail must measure retrieval over a fully-embedded catalog.
    EmbeddingService(session, embedder, _MODEL_VERSION).embed_missing(limit=None)
    service = RecommendationService(
        session,
        embed_provider=embedder,
        embed_model_version=_MODEL_VERSION,
        chat_llm=None,
    )

    facets = service._facets(profile_id)
    facet_recs = service.recommend(profile_id, taste=MIXED_TASTE, k=k, swing_slots=0, vote_mix=True)
    facet_a, facet_b = _cousin_counts(facet_recs, a_cousins, b_cousins)

    # Force the historical single averaged centroid by overwriting the facet cache with one facet.
    centroid = compute_taste_centroid(session, profile_id, _MODEL_VERSION)
    assert centroid is not None  # the persona always has signal
    service._facets_cache[profile_id] = [
        Facet(centroid=centroid, weight=1.0, size=2 * _SEEDS_PER_MODE, mean_intra_sim=1.0)
    ]
    single_recs = service.recommend(
        profile_id, taste=MIXED_TASTE, k=k, swing_slots=0, vote_mix=True
    )
    single_a, single_b = _cousin_counts(single_recs, a_cousins, b_cousins)

    return FacetDiscriminationResult(
        facet_count=len(facets),
        facet_sizes=[f.size for f in facets],
        facet_a_cousins=facet_a,
        facet_b_cousins=facet_b,
        single_a_cousins=single_a,
        single_b_cousins=single_b,
    )
