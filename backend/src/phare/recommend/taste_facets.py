"""Multi-facet taste retrieval — split a profile's taste into a few distinct *modes* instead of one
averaged centroid.

The single centroid is the mean of every positively-signalled title vector. A real profile spans
distinct taste modes (cerebral sci-fi + dark action + prestige drama); averaging them yields a
blurry mid-point that is *near none of them*, so retrieval fetches generic mid-space titles and the
genuinely on-taste candidates from each mode lose (measured across rounds 5–9). This module clusters
the *positive* contributions into ``k`` facets (k adaptive, 1–``_MAX_FACETS``), each with its own
centroid and a share of the retrieval budget proportional to its event mass.

Design constraints (see ``CLAUDE.md`` principle 5 and the round-10 mission):

- **Deterministic.** pgvector HNSW is already approximate; the clustering must not add a second
  source of run-to-run flicker. So: contributions arrive in a stable ``title_id`` order (see
  ``taste_vector.taste_contributions``), initialisation is farthest-point from that order, and
  Lloyd iterations are fixed-count — no RNG anywhere.
- **k=1 reproduces today.** Small histories (< ``_MIN_TITLES_FOR_SPLIT`` positive titles) and
  already-cohesive tastes collapse to a single facet whose centroid equals the historical one — the
  N=1 degradation path is untouched.
- **Negatives stay global.** Abandonment / dislike signals push the *whole* taste away, not one
  mode; they're not clustered, they ride along in every facet centroid via the blended contribution
  math. Only positive contributions define the facets (what you like has structure; what you avoid
  is a blanket).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from phare.recommend.taste_vector import (
    TasteContribution,
    blend_contributions,
)

logger = logging.getLogger(__name__)

# Below this many positively-weighted titles a profile has too little structure to split honestly —
# clustering 3 vectors into 3 facets is noise, not taste modes. Collapses to a single facet, which
# reproduces the historical single-centroid behaviour exactly (principle 5, graceful N=1).
_MIN_TITLES_FOR_SPLIT = 8
# Ceiling on facets. A slate is ~12–20 items; past ~4 modes the per-facet retrieval budget gets
# too thin to be worth a separate ANN query, and real tastes rarely fragment further. Also caps ANN
# cost at 4× a single query in the worst case — the merged pool total stays ~the single-centroid
# total (budget is split, not multiplied — see ``facet_budgets``).
_MAX_FACETS = 4
# Stop splitting once a candidate cluster's mean intra-similarity (cosine of members to the cluster
# centroid) is at/above this — the members already point the same way, so a further split would
# carve a cohesive mode in half. Tuned against the real embedder's compressed cosine band: genuinely
# distinct modes sit well below this, one coherent mode well above.
_COHESION_THRESHOLD = 0.92
# Fixed Lloyd iterations. k-means converges fast on a handful of points; a fixed small count keeps
# it deterministic (no convergence-tolerance branch differing across float orders) and bounded.
_LLOYD_ITERS = 8
# Per-facet retrieval floor as a fraction of the single-centroid limit. A facet backed by only 10%
# of the history still needs *enough* candidates to contribute a couple of picks, so its share never
# drops below this fraction of the total even if its weight is tiny (the rest is split by weight).
_FACET_LIMIT_FLOOR_FRAC = 0.25


@dataclass(frozen=True)
class Facet:
    """One taste mode: its centroid (the query vector), the share of positive event mass behind it
    (``weight``, normalised across facets to sum to 1), and how many titles seeded it. ``weight`` is
    what sizes the facet's retrieval budget; ``size`` is for observability."""

    centroid: list[float]
    weight: float
    size: int
    mean_intra_sim: float


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two raw vectors (retrieval ranks by cosine, so cohesion must too)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _assign(
    contributions: list[TasteContribution], centroids: list[list[float]]
) -> list[list[TasteContribution]]:
    """Assign each contribution to its nearest (max-cosine) centroid. Ties break to the lowest
    centroid index — deterministic, and the contributions themselves arrive pre-sorted."""
    clusters: list[list[TasteContribution]] = [[] for _ in centroids]
    for c in contributions:
        best_idx = 0
        best_sim = -2.0
        for idx, centroid in enumerate(centroids):
            sim = _cosine(c.embedding, centroid)
            if sim > best_sim:
                best_sim = sim
                best_idx = idx
        clusters[best_idx].append(c)
    return clusters


def _farthest_point_init(contributions: list[TasteContribution], k: int) -> list[list[float]]:
    """Deterministic k seeds by farthest-point (max-min cosine distance) from a stable start.

    Seed 0 is the first contribution in the (title-id-sorted) order — no RNG. Each subsequent seed
    is the contribution *least similar* to the seeds chosen so far, so the seeds land on distinct
    modes rather than clumping. Ties break to the earlier contribution in the stable order."""
    seeds: list[list[float]] = [list(contributions[0].embedding)]
    while len(seeds) < k:
        best_c: TasteContribution | None = None
        best_score = 2.0  # we minimise the max-similarity-to-any-seed; lower = farther
        for c in contributions:
            max_sim = max(_cosine(c.embedding, s) for s in seeds)
            if max_sim < best_score:
                best_score = max_sim
                best_c = c
        if best_c is None:  # pragma: no cover - contributions is non-empty here
            break
        seeds.append(list(best_c.embedding))
    return seeds


def _kmeans(contributions: list[TasteContribution], k: int) -> list[list[TasteContribution]]:
    """Deterministic Lloyd's k-means over the contribution embeddings, farthest-point seeded, fixed
    iteration count, weight-blended centroids. Returns the non-empty clusters."""
    centroids = _farthest_point_init(contributions, k)
    clusters = _assign(contributions, centroids)
    for _ in range(_LLOYD_ITERS):
        new_centroids: list[list[float]] = []
        for cluster, prev in zip(clusters, centroids, strict=True):
            blended = blend_contributions(cluster) if cluster else None
            new_centroids.append(blended if blended is not None else prev)
        reassigned = _assign(contributions, new_centroids)
        if [[c.title_id for c in cl] for cl in reassigned] == [
            [c.title_id for c in cl] for cl in clusters
        ]:
            clusters = reassigned
            break
        clusters = reassigned
    return [cl for cl in clusters if cl]


def _mean_intra_sim(cluster: list[TasteContribution], centroid: list[float]) -> float:
    """Mean cosine of a cluster's members to its centroid — how cohesive the mode is (1 = identical
    directions). A single-member cluster is perfectly cohesive by definition."""
    if not cluster:
        return 1.0
    return sum(_cosine(c.embedding, centroid) for c in cluster) / len(cluster)


def _least_cohesive_k(
    contributions: list[TasteContribution],
) -> list[list[TasteContribution]]:
    """Pick k by growing it until every cluster is cohesive (or ``_MAX_FACETS`` is hit).

    Start at k=2 and increase: at each k, run k-means and check the *least* cohesive cluster's mean
    intra-similarity. Once even the worst cluster clears ``_COHESION_THRESHOLD`` the modes are
    cleanly separated — stop. This is silhouette-lite: cheap, deterministic, and it stops as soon as
    splitting stops buying separation, so a two-mode taste yields 2 facets and a coherent one would
    have already been caught by the k=1 cohesion check upstream."""
    best_clusters = _kmeans(contributions, 2)
    for k in range(2, _MAX_FACETS + 1):
        clusters = _kmeans(contributions, k)
        if len(clusters) < k:  # k-means collapsed empties — no point growing further
            return clusters
        worst = min(
            _mean_intra_sim(cl, blend_contributions(cl) or cl[0].embedding) for cl in clusters
        )
        best_clusters = clusters
        if worst >= _COHESION_THRESHOLD:
            break
    return best_clusters


def extract_facets(contributions: list[TasteContribution]) -> list[Facet]:
    """Cluster the *positive* contributions into 1–``_MAX_FACETS`` taste facets.

    k=1 (a single facet whose centroid is the historical taste centroid) whenever the profile is too
    small to split (< ``_MIN_TITLES_FOR_SPLIT`` positive titles) or its taste is already cohesive
    (the whole set's mean intra-similarity at/above ``_COHESION_THRESHOLD``). Otherwise it grows k
    until the modes separate. Negative contributions are *not* clustered — they ride into every
    facet's centroid via the blend, pushing the whole taste away from what's avoided (see module
    docstring). Returns ``[]`` only when there's no usable signal at all.

    Facet ``weight`` is the share of *positive* event mass behind the facet, normalised to sum to 1,
    so a mode backed by 60% of the history gets ~60% of the retrieval budget (``facet_budgets``)."""
    if not contributions:
        return []
    positives = [c for c in contributions if c.weight > 0.0]
    negatives = [c for c in contributions if c.weight < 0.0]
    # Negatives blend into every facet centroid — reconstruct the full contribution set per cluster.
    full_centroid = blend_contributions(contributions)
    if full_centroid is None:  # positives and negatives cancelled — no direction to search
        return []

    def _facet_from(cluster_positives: list[TasteContribution]) -> Facet:
        members = [*cluster_positives, *negatives]
        centroid = blend_contributions(members) or full_centroid
        pos_mass = sum(c.weight for c in cluster_positives)
        return Facet(
            centroid=centroid,
            weight=pos_mass,
            size=len(cluster_positives),
            mean_intra_sim=_mean_intra_sim(
                cluster_positives, blend_contributions(cluster_positives) or centroid
            ),
        )

    single = [_facet_from(positives)]
    if len(positives) < _MIN_TITLES_FOR_SPLIT:
        return _normalise_weights(single)
    whole_cohesion = _mean_intra_sim(positives, blend_contributions(positives) or full_centroid)
    if whole_cohesion >= _COHESION_THRESHOLD:
        return _normalise_weights(single)

    clusters = _least_cohesive_k(positives)
    if len(clusters) <= 1:
        return _normalise_weights(single)
    facets = _normalise_weights([_facet_from(cl) for cl in clusters])
    # Stable, meaningful order: biggest mode first (so per-facet budget rounding favours it).
    facets.sort(key=lambda f: (-f.weight, -f.size))
    return facets


def _normalise_weights(facets: list[Facet]) -> list[Facet]:
    """Renormalise facet weights to sum to 1 (positive event mass shares); all-zero = equal."""
    total = sum(f.weight for f in facets)
    if total <= 0.0:
        equal = 1.0 / len(facets)
        return [Facet(f.centroid, equal, f.size, f.mean_intra_sim) for f in facets]
    return [Facet(f.centroid, f.weight / total, f.size, f.mean_intra_sim) for f in facets]


def facet_budgets(facets: list[Facet], total_limit: int) -> list[int]:
    """Split ``total_limit`` candidates across facets by weight, with a per-facet floor.

    The merged pool must stay ~the single-centroid size (``k*4+10``), NOT ``k`` facets × that — so
    the budget is *divided*, not multiplied. Each facet gets at least ``_FACET_LIMIT_FLOOR_FRAC`` of
    an equal share (a thin 10% mode still retrieves enough to place a pick or two), the rest split
    by weight, and largest-remainder rounding hands out the leftover so the parts sum to
    ``total_limit``. Single facet → the whole limit (identical to today)."""
    n = len(facets)
    if n <= 1:
        return [total_limit]
    floor = max(1, int(_FACET_LIMIT_FLOOR_FRAC * total_limit / n))
    floored_total = floor * n
    remaining = max(0, total_limit - floored_total)
    raw = [floor + remaining * f.weight for f in facets]
    budgets = [int(x) for x in raw]
    leftover = total_limit - sum(budgets)
    order = sorted(range(n), key=lambda i: raw[i] - budgets[i], reverse=True)
    for i in range(max(0, leftover)):
        budgets[order[i % n]] += 1
    return budgets


def log_facets(profile_id: str, facets: list[Facet]) -> None:
    """Structured observability on the facet split (mission point 6)."""
    logger.info(
        "taste.facets",
        extra={
            "profile_id": profile_id,
            "k": len(facets),
            "sizes": [f.size for f in facets],
            "weights": [round(f.weight, 3) for f in facets],
            "mean_intra_sim": [round(f.mean_intra_sim, 3) for f in facets],
        },
    )
