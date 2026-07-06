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
- **Vectorized.** The clustering runs on numpy (contributions stacked once into an ``(n, d)``
  float64 matrix, cosine = row-normalised matmul). The pure-Python original cost ~36 s per call on
  a real ~900-title history; this is the same algorithm with the same tie-break semantics —
  ``argmax``/``argmin`` pick the first extremum, exactly like the strict ``>``/``<`` comparisons
  they replace — verified by a reference-implementation parity test.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from phare.recommend.taste_vector import TasteContribution

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
# Per-facet retrieval depth floor (live round-10 finding). The first cut split the single-query
# budget (k*4+10) proportionally across facets, which left the smallest facet ~11 candidates — too
# shallow for its picks to survive runtime/genre filters, the quality floor, and MMR. ANN queries
# are cheap (each is one indexed pgvector scan), so every facet retrieves at least this deep: the
# max of 2*k and this constant. Slate share is enforced downstream by the reranker's facet quota,
# not by starving retrieval. Worst case (4 facets) is ~4 ANN queries of ~2k each — bounded, and the
# merged pool stays the same order of magnitude as the historical single query.
_FACET_MIN_DEPTH = 24


@dataclass(frozen=True)
class Facet:
    """One taste mode: its centroid (the query vector), the share of positive event mass behind it
    (``weight``, normalised across facets to sum to 1), and how many titles seeded it. ``weight`` is
    what sizes the facet's retrieval budget; ``size`` is for observability.

    ``member_title_ids`` are the *positive* contributions that seeded the facet — what makes the
    facet inspectable (principle 2): the taste API joins them back to titles for genre labels and
    exemplars. Negatives ride into the centroid but are never members."""

    centroid: list[float]
    weight: float
    size: int
    mean_intra_sim: float
    member_title_ids: tuple[uuid.UUID, ...] = ()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two raw vectors (retrieval ranks by cosine, so cohesion must too)."""
    av = np.asarray(a, dtype=np.float64)
    bv = np.asarray(b, dtype=np.float64)
    na = float(np.linalg.norm(av))
    nb = float(np.linalg.norm(bv))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(av @ bv) / (na * nb)


def _stack(contributions: list[TasteContribution]) -> np.ndarray:
    """The contributions' embeddings as one ``(n, d)`` float64 matrix (``(0, 0)`` when empty)."""
    if not contributions:
        return np.zeros((0, 0), dtype=np.float64)
    return np.array([c.embedding for c in contributions], dtype=np.float64)


def _unit_rows(matrix: np.ndarray) -> np.ndarray:
    """Rows scaled to unit norm; all-zero rows stay zero (their cosine to anything is 0.0, matching
    the historical zero-norm guard)."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0.0, 1.0, norms)


def _assign(unit: np.ndarray, centroids: np.ndarray) -> np.ndarray:
    """Nearest (max-cosine) centroid label per row of the unit matrix. Ties break to the lowest
    centroid index (``argmax`` returns the first maximum — same as the historical strict ``>``),
    and the contributions themselves arrive pre-sorted."""
    return np.argmax(unit @ _unit_rows(centroids).T, axis=1)


def _farthest_point_init(unit: np.ndarray, k: int) -> np.ndarray:
    """Deterministic k seed *indices* by farthest-point (max-min cosine distance) from a stable
    start.

    Seed 0 is the first contribution in the (title-id-sorted) order — no RNG. Each subsequent seed
    is the contribution *least similar* to the seeds chosen so far, so the seeds land on distinct
    modes rather than clumping. Ties break to the earlier contribution in the stable order
    (``argmin`` returns the first minimum — same as the historical strict ``<``)."""
    seeds = [0]
    max_sims = unit @ unit[0]  # per-row max similarity to any seed so far
    while len(seeds) < k:
        nxt = int(np.argmin(max_sims))
        seeds.append(nxt)
        max_sims = np.maximum(max_sims, unit @ unit[nxt])
    return np.array(seeds, dtype=np.intp)


def _blend_rows(raw: np.ndarray, weights: np.ndarray, idx: np.ndarray) -> np.ndarray | None:
    """Signed, weight-normalised blend of the selected rows — the centroid of a cluster. Uses
    ``sum |weight|`` as the denominator (a mix of positive and negative signals must not cancel the
    scale), the same math as ``taste_vector.blend_contributions``. ``None`` if the weights net to
    nothing (or ``idx`` is empty)."""
    total_abs = float(np.abs(weights[idx]).sum())
    if total_abs == 0.0:
        return None
    return weights[idx] @ raw[idx] / total_abs


def _kmeans(unit: np.ndarray, raw: np.ndarray, weights: np.ndarray, k: int) -> list[np.ndarray]:
    """Deterministic Lloyd's k-means over the contribution embeddings, farthest-point seeded, fixed
    iteration count, weight-blended centroids. Returns the non-empty clusters as index arrays (in
    contribution order — ``flatnonzero`` is ascending, same as the historical append-in-order)."""
    seeds = raw[_farthest_point_init(unit, k)]
    labels = _assign(unit, seeds)
    for _ in range(_LLOYD_ITERS):
        # An empty (or net-zero) cluster keeps its original seed as centroid — the historical code
        # zipped against the *initial* centroid list, never the previous iteration's.
        centroids = seeds.copy()
        for j in range(k):
            blended = _blend_rows(raw, weights, np.flatnonzero(labels == j))
            if blended is not None:
                centroids[j] = blended
        new_labels = _assign(unit, centroids)
        converged = bool(np.array_equal(new_labels, labels))
        labels = new_labels
        if converged:
            break
    clusters = [np.flatnonzero(labels == j) for j in range(k)]
    return [idx for idx in clusters if idx.size]


def _mean_intra_sim(unit_members: np.ndarray, centroid: np.ndarray) -> float:
    """Mean cosine of a cluster's members to its centroid — how cohesive the mode is (1 = identical
    directions). A single-member cluster is perfectly cohesive by definition; an empty one is 1.0
    and a zero-norm centroid reads 0.0, matching the historical per-pair zero guard."""
    if unit_members.shape[0] == 0:
        return 1.0
    norm = float(np.linalg.norm(centroid))
    if norm == 0.0:
        return 0.0
    return float((unit_members @ (np.asarray(centroid, dtype=np.float64) / norm)).mean())


def _cluster_cohesion(
    unit: np.ndarray, raw: np.ndarray, weights: np.ndarray, idx: np.ndarray
) -> float:
    """A cluster's mean intra-similarity to its own blended centroid (first member as the fallback
    anchor when the blend nets to nothing — the historical ``or cl[0].embedding``)."""
    centroid = _blend_rows(raw, weights, idx)
    if centroid is None:
        centroid = raw[idx[0]]
    return _mean_intra_sim(unit[idx], centroid)


def _least_cohesive_k(unit: np.ndarray, raw: np.ndarray, weights: np.ndarray) -> list[np.ndarray]:
    """Pick k by growing it until every cluster is cohesive (or ``_MAX_FACETS`` is hit).

    Start at k=2 and increase: at each k, run k-means and check the *least* cohesive cluster's mean
    intra-similarity. Once even the worst cluster clears ``_COHESION_THRESHOLD`` the modes are
    cleanly separated — stop. This is silhouette-lite: cheap, deterministic, and it stops as soon as
    splitting stops buying separation, so a two-mode taste yields 2 facets and a coherent one would
    have already been caught by the k=1 cohesion check upstream."""
    best_clusters: list[np.ndarray] = []
    for k in range(2, _MAX_FACETS + 1):
        clusters = _kmeans(unit, raw, weights, k)
        if len(clusters) < k:  # k-means collapsed empties — no point growing further
            return clusters
        worst = min(_cluster_cohesion(unit, raw, weights, idx) for idx in clusters)
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
    raw = _stack(contributions)
    weights = np.array([c.weight for c in contributions], dtype=np.float64)
    pos_rows = np.flatnonzero(weights > 0.0)
    neg_rows = np.flatnonzero(weights < 0.0)
    positives = [contributions[i] for i in pos_rows]
    # Negatives blend into every facet centroid — reconstruct the full contribution set per cluster.
    full = _blend_rows(raw, weights, np.arange(len(contributions)))
    if full is None:  # positives and negatives cancelled — no direction to search
        return []
    full_centroid = full.tolist()
    pos_raw = raw[pos_rows]
    pos_unit = _unit_rows(pos_raw)
    pos_weights = weights[pos_rows]

    def _facet_from(cluster: np.ndarray) -> Facet:
        # ``cluster`` indexes into the positives; blend positives + every negative for the centroid.
        member_rows = pos_rows[cluster]
        blended = _blend_rows(raw, weights, np.concatenate([member_rows, neg_rows]))
        centroid = blended.tolist() if blended is not None else full_centroid
        # Cohesion is measured against the *positives-only* blend (the mode itself, negatives
        # excluded), falling back to the facet centroid — the historical anchor choice.
        own = _blend_rows(pos_raw, pos_weights, cluster)
        anchor = own if own is not None else np.asarray(centroid, dtype=np.float64)
        cluster_positives = [positives[i] for i in cluster]
        return Facet(
            centroid=centroid,
            weight=sum(c.weight for c in cluster_positives),
            size=len(cluster_positives),
            mean_intra_sim=_mean_intra_sim(pos_unit[cluster], anchor),
            member_title_ids=tuple(c.title_id for c in cluster_positives),
        )

    all_positives = np.arange(len(positives), dtype=np.intp)
    single = [_facet_from(all_positives)]
    if len(positives) < _MIN_TITLES_FOR_SPLIT:
        return _normalise_weights(single)
    whole_blend = _blend_rows(pos_raw, pos_weights, all_positives)
    whole_anchor = whole_blend if whole_blend is not None else np.asarray(full_centroid)
    if _mean_intra_sim(pos_unit, whole_anchor) >= _COHESION_THRESHOLD:
        return _normalise_weights(single)

    clusters = _least_cohesive_k(pos_unit, pos_raw, pos_weights)
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
        return [
            Facet(f.centroid, equal, f.size, f.mean_intra_sim, f.member_title_ids) for f in facets
        ]
    return [
        Facet(f.centroid, f.weight / total, f.size, f.mean_intra_sim, f.member_title_ids)
        for f in facets
    ]


def facet_budgets(facets: list[Facet], k: int) -> list[int]:
    """Per-facet ANN retrieval limits for a slate of ``k``.

    Single facet → the historical ``k*4+10`` (byte-identical behaviour). Multiple facets: each gets
    its weight's share of that total, floored at ``max(2*k, _FACET_MIN_DEPTH)`` — the proportional
    split alone proved too shallow live (a 4-facet profile left the smallest facet ~11 candidates,
    which the filters + quality floor + MMR then erased). Depth is cheap (one indexed pgvector scan
    per facet); *slate share* is what proportionality governs, and that's enforced by the reranker's
    facet quota, not by starving a facet's retrieval. Deterministic: pure arithmetic on weights."""
    total = k * 4 + 10
    if len(facets) <= 1:
        return [total]
    floor = max(2 * k, _FACET_MIN_DEPTH)
    return [max(int(f.weight * total), floor) for f in facets]


def rank_members_by_centrality(
    facet: Facet, contributions: list[TasteContribution]
) -> list[uuid.UUID]:
    """The facet's member title ids, most central first (cosine to the facet centroid, descending).

    Feeds the taste API's *exemplars* — the titles that best typify a taste mode. Ties break on the
    stable ``title_id`` order (same anchor the clustering uses), so the ranking is deterministic.
    Members whose contribution is missing from ``contributions`` are skipped (defensive: the two
    always come from the same extraction in practice)."""
    by_id = {c.title_id: c for c in contributions}
    members = [by_id[tid] for tid in facet.member_title_ids if tid in by_id]
    members.sort(key=lambda c: (-_cosine(c.embedding, facet.centroid), c.title_id.bytes))
    return [c.title_id for c in members]


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
