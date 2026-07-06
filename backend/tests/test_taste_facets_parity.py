"""Parity + performance regression for the numpy taste-facet clustering.

The facet extraction was rewritten from pure-Python cosine loops (~36 s per call on a real ~900
title history) onto numpy. The clustering feeds retrieval, so it must stay *deterministic and
behaviourally identical*: same farthest-point init from the stable title-id order, same
first-extremum tie-breaks (numpy ``argmax``/``argmin`` pick the first max/min, matching the strict
``>``/``<`` comparisons of the original), same adaptive-k stopping rule.

The reference implementation below is the pre-numpy code, copied verbatim. The parity tests run
both over deterministic synthetic fixtures (fixed arithmetic, no RNG — flaky vectors would defeat
the point) and assert identical cluster memberships, weights, and facet order.
"""

from __future__ import annotations

import math
import time
import uuid

from phare.recommend.taste_facets import (
    _COHESION_THRESHOLD,
    _LLOYD_ITERS,
    _MAX_FACETS,
    _MIN_TITLES_FOR_SPLIT,
    Facet,
    extract_facets,
)
from phare.recommend.taste_vector import TasteContribution, blend_contributions

# --- Reference implementation: the pure-Python original, verbatim ------------------------------


def _ref_cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _ref_assign(
    contributions: list[TasteContribution], centroids: list[list[float]]
) -> list[list[TasteContribution]]:
    clusters: list[list[TasteContribution]] = [[] for _ in centroids]
    for c in contributions:
        best_idx = 0
        best_sim = -2.0
        for idx, centroid in enumerate(centroids):
            sim = _ref_cosine(c.embedding, centroid)
            if sim > best_sim:
                best_sim = sim
                best_idx = idx
        clusters[best_idx].append(c)
    return clusters


def _ref_farthest_point_init(contributions: list[TasteContribution], k: int) -> list[list[float]]:
    seeds: list[list[float]] = [list(contributions[0].embedding)]
    while len(seeds) < k:
        best_c: TasteContribution | None = None
        best_score = 2.0
        for c in contributions:
            max_sim = max(_ref_cosine(c.embedding, s) for s in seeds)
            if max_sim < best_score:
                best_score = max_sim
                best_c = c
        if best_c is None:
            break
        seeds.append(list(best_c.embedding))
    return seeds


def _ref_kmeans(contributions: list[TasteContribution], k: int) -> list[list[TasteContribution]]:
    centroids = _ref_farthest_point_init(contributions, k)
    clusters = _ref_assign(contributions, centroids)
    for _ in range(_LLOYD_ITERS):
        new_centroids: list[list[float]] = []
        for cluster, prev in zip(clusters, centroids, strict=True):
            blended = blend_contributions(cluster) if cluster else None
            new_centroids.append(blended if blended is not None else prev)
        reassigned = _ref_assign(contributions, new_centroids)
        if [[c.title_id for c in cl] for cl in reassigned] == [
            [c.title_id for c in cl] for cl in clusters
        ]:
            clusters = reassigned
            break
        clusters = reassigned
    return [cl for cl in clusters if cl]


def _ref_mean_intra_sim(cluster: list[TasteContribution], centroid: list[float]) -> float:
    if not cluster:
        return 1.0
    return sum(_ref_cosine(c.embedding, centroid) for c in cluster) / len(cluster)


def _ref_least_cohesive_k(
    contributions: list[TasteContribution],
) -> list[list[TasteContribution]]:
    best_clusters = _ref_kmeans(contributions, 2)
    for k in range(2, _MAX_FACETS + 1):
        clusters = _ref_kmeans(contributions, k)
        if len(clusters) < k:
            return clusters
        worst = min(
            _ref_mean_intra_sim(cl, blend_contributions(cl) or cl[0].embedding) for cl in clusters
        )
        best_clusters = clusters
        if worst >= _COHESION_THRESHOLD:
            break
    return best_clusters


def _ref_normalise_weights(facets: list[Facet]) -> list[Facet]:
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


def ref_extract_facets(contributions: list[TasteContribution]) -> list[Facet]:
    if not contributions:
        return []
    positives = [c for c in contributions if c.weight > 0.0]
    negatives = [c for c in contributions if c.weight < 0.0]
    full_centroid = blend_contributions(contributions)
    if full_centroid is None:
        return []

    def _facet_from(cluster_positives: list[TasteContribution]) -> Facet:
        members = [*cluster_positives, *negatives]
        centroid = blend_contributions(members) or full_centroid
        pos_mass = sum(c.weight for c in cluster_positives)
        return Facet(
            centroid=centroid,
            weight=pos_mass,
            size=len(cluster_positives),
            mean_intra_sim=_ref_mean_intra_sim(
                cluster_positives, blend_contributions(cluster_positives) or centroid
            ),
            member_title_ids=tuple(c.title_id for c in cluster_positives),
        )

    single = [_facet_from(positives)]
    if len(positives) < _MIN_TITLES_FOR_SPLIT:
        return _ref_normalise_weights(single)
    whole_cohesion = _ref_mean_intra_sim(positives, blend_contributions(positives) or full_centroid)
    if whole_cohesion >= _COHESION_THRESHOLD:
        return _ref_normalise_weights(single)

    clusters = _ref_least_cohesive_k(positives)
    if len(clusters) <= 1:
        return _ref_normalise_weights(single)
    facets = _ref_normalise_weights([_facet_from(cl) for cl in clusters])
    facets.sort(key=lambda f: (-f.weight, -f.size))
    return facets


# --- Deterministic fixtures ---------------------------------------------------------------------


def _contribution(i: int, embedding: list[float], weight: float) -> TasteContribution:
    # uuid.UUID(int=i) is monotonic in ``.bytes`` — the fixture arrives pre-sorted by title_id,
    # matching the stable order ``taste_contributions`` guarantees in production.
    return TasteContribution(title_id=uuid.UUID(int=i), embedding=embedding, weight=weight)


def _mode_vector(dim: int, lo: int, hi: int, i: int) -> list[float]:
    """A vector concentrated on dims [lo, hi) with deterministic per-title jitter (no RNG)."""
    return [
        (1.0 + 0.07 * ((i * 7 + j * 3) % 5)) if lo <= j < hi else 0.02 * ((i * 11 + j * 5) % 3)
        for j in range(dim)
    ]


def _multi_mode_history() -> list[TasteContribution]:
    """~50 contributions across three orthogonal-ish taste modes + a few negatives — the shape a
    real multi-mode profile has, small enough that the pure-Python reference stays fast."""
    dim = 32
    contributions: list[TasteContribution] = []
    i = 0
    for _ in range(20):  # mode A, dims 0-9, mixed weights
        contributions.append(_contribution(i, _mode_vector(dim, 0, 10, i), 0.5 + 0.25 * (i % 4)))
        i += 1
    for _ in range(18):  # mode B, dims 10-19
        contributions.append(_contribution(i, _mode_vector(dim, 10, 20, i), 0.6 + 0.2 * (i % 3)))
        i += 1
    for _ in range(8):  # mode C, dims 20-27 — the light mode
        contributions.append(_contribution(i, _mode_vector(dim, 20, 28, i), 1.0))
        i += 1
    for _ in range(4):  # negatives near mode A — ride into centroids, never cluster
        contributions.append(_contribution(i, _mode_vector(dim, 0, 10, i), -0.8))
        i += 1
    return contributions


def _facet_fingerprint(facets: list[Facet]) -> list[tuple]:
    """Everything behaviour-relevant about a facet split, floats rounded to absorb the low-bit
    differences between sequential-Python and numpy summation (the algorithms are identical; the
    float *order of operations* is not, and cannot be)."""
    return [
        (
            f.member_title_ids,
            f.size,
            round(f.weight, 9),
            round(f.mean_intra_sim, 9),
            tuple(round(x, 9) for x in f.centroid),
        )
        for f in facets
    ]


def test_numpy_matches_reference_on_a_multi_mode_history() -> None:
    contributions = _multi_mode_history()
    ours = extract_facets(contributions)
    reference = ref_extract_facets(contributions)
    assert len(ours) > 1  # the fixture genuinely splits — otherwise the test proves nothing
    assert _facet_fingerprint(ours) == _facet_fingerprint(reference)


def test_numpy_matches_reference_on_degenerate_histories() -> None:
    dim = 16
    cases: list[list[TasteContribution]] = [
        [],  # no signal at all
        [_contribution(0, _mode_vector(dim, 0, 8, 0), 1.0)],  # N=1
        # Below the split floor: two clear modes must still collapse to one facet.
        [
            _contribution(i, _mode_vector(dim, 0 if i % 2 else 8, 8 if i % 2 else 16, i), 1.0)
            for i in range(_MIN_TITLES_FOR_SPLIT - 2)
        ],
        # Cohesive: enough titles, all one mode.
        [
            _contribution(i, _mode_vector(dim, 0, 8, i), 1.0)
            for i in range(_MIN_TITLES_FOR_SPLIT + 4)
        ],
        # Negatives only — positives and negatives can't both be empty for a usable blend.
        [_contribution(i, _mode_vector(dim, 0, 8, i), -1.0) for i in range(5)],
        # Identical vectors (tie-break stress: farthest-point degenerates to duplicate seeds).
        [_contribution(i, [1.0] * dim, 1.0) for i in range(_MIN_TITLES_FOR_SPLIT + 2)],
    ]
    for contributions in cases:
        assert _facet_fingerprint(extract_facets(contributions)) == _facet_fingerprint(
            ref_extract_facets(contributions)
        )


def test_extraction_is_fast_at_production_scale() -> None:
    # 400 contributions × 1536 dims — realistic history against the real embedding width. The
    # pure-Python original took ~10 s at this size (36 s at ~900 titles); the numpy version runs in
    # tens of milliseconds. The bound is deliberately loose for slow CI machines: even 2 s would
    # still be a 20× regression margin below the old cost.
    dim = 1536
    contributions: list[TasteContribution] = []
    for i in range(400):
        mode = i % 3
        lo = mode * 512
        vec = [
            (1.0 + 0.05 * ((i * 7 + j * 11) % 13))
            if lo <= j < lo + 512
            else 0.01 * ((i * 3 + j) % 7)
            for j in range(dim)
        ]
        contributions.append(_contribution(i, vec, -1.0 if i % 29 == 0 else 1.0 + 0.25 * (i % 3)))

    start = time.perf_counter()
    facets = extract_facets(contributions)
    elapsed = time.perf_counter() - start

    assert facets  # it did real work
    assert len(facets) > 1  # and the fixture exercised the clustering path, not the k=1 shortcut
    assert elapsed < 2.0, f"facet extraction took {elapsed:.2f}s — the numpy path has regressed"
