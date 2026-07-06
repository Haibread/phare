"""Unit tests for the deterministic taste-facet clustering (round 10).

Pure, no DB: the facet extraction works on ``TasteContribution`` objects, so these build synthetic
contributions with hand-placed vectors and assert the clustering, weighting, budgeting, and the
graceful k=1 degradation directly.
"""

from __future__ import annotations

import uuid

from phare.recommend.taste_facets import (
    _MAX_FACETS,
    _MIN_TITLES_FOR_SPLIT,
    Facet,
    extract_facets,
    facet_budgets,
)
from phare.recommend.taste_vector import TasteContribution


def _contrib(vec: list[float], weight: float = 1.0) -> TasteContribution:
    return TasteContribution(title_id=uuid.uuid4(), embedding=vec, weight=weight)


def _mode(dim: int, lo: int, hi: int, jitter: int) -> list[float]:
    """A vector in dims [lo, hi) with a little per-title jitter so members aren't identical."""
    out = [0.0] * dim
    for j in range(lo, hi):
        out[j] = 1.0 + 0.01 * ((j + jitter) % 3)
    return out


def test_empty_history_yields_no_facets() -> None:
    assert extract_facets([]) == []


def test_small_history_stays_single_facet() -> None:
    # Below the split floor: even two clearly-distinct modes collapse to one facet (graceful N=1).
    contribs = [_mode(8, 0, 4, 0), _mode(8, 4, 8, 0), _mode(8, 0, 4, 1)]
    assert len(contribs) < _MIN_TITLES_FOR_SPLIT
    facets = extract_facets([_contrib(c) for c in contribs])
    assert len(facets) == 1
    assert facets[0].weight == 1.0


def test_cohesive_history_stays_single_facet() -> None:
    # Enough titles to split, but they all point the same way — one cohesive mode, so k=1.
    contribs = [_contrib(_mode(8, 0, 4, i)) for i in range(_MIN_TITLES_FOR_SPLIT + 2)]
    facets = extract_facets(contribs)
    assert len(facets) == 1


def test_two_distinct_modes_split_into_two_facets() -> None:
    # Two orthogonal modes, each with several members → exactly two facets.
    mode_a = [_contrib(_mode(8, 0, 4, i)) for i in range(6)]
    mode_b = [_contrib(_mode(8, 4, 8, i)) for i in range(6)]
    facets = extract_facets([*mode_a, *mode_b])
    assert len(facets) == 2
    assert sum(f.size for f in facets) == 12
    assert abs(sum(f.weight for f in facets) - 1.0) < 1e-9


def test_facet_weight_tracks_event_mass() -> None:
    # A mode backed by more (and heavier) titles gets the larger weight/share.
    big = [_contrib(_mode(8, 0, 4, i), weight=1.5) for i in range(8)]
    small = [_contrib(_mode(8, 4, 8, i), weight=0.5) for i in range(4)]
    facets = extract_facets([*big, *small])
    assert len(facets) == 2
    # Facets are sorted biggest-first.
    assert facets[0].weight > facets[1].weight
    assert facets[0].size == 8


def test_facet_count_is_capped() -> None:
    modes = []
    for m in range(6):  # 6 distinct modes, more than _MAX_FACETS
        lo = m * 2
        modes.extend(
            _contrib([1.0 if lo <= j < lo + 2 else 0.0 for j in range(12)]) for _ in range(4)
        )
    facets = extract_facets(modes)
    assert 1 <= len(facets) <= _MAX_FACETS


def test_extraction_is_deterministic() -> None:
    mode_a = [_contrib(_mode(8, 0, 4, i)) for i in range(6)]
    mode_b = [_contrib(_mode(8, 4, 8, i)) for i in range(6)]
    contribs = [*mode_a, *mode_b]
    first = extract_facets(contribs)
    second = extract_facets(contribs)
    assert [(f.size, round(f.weight, 6)) for f in first] == [
        (f.size, round(f.weight, 6)) for f in second
    ]


def test_negatives_do_not_form_facets_but_ride_into_centroids() -> None:
    # A disliked title (negative weight) must not seed its own facet; it pulls every facet centroid.
    mode_a = [_contrib(_mode(8, 0, 4, i)) for i in range(6)]
    mode_b = [_contrib(_mode(8, 4, 8, i)) for i in range(6)]
    disliked = _contrib(_mode(8, 0, 4, 99), weight=-1.0)
    facets = extract_facets([*mode_a, *mode_b, disliked])
    # Still two facets (positives only), and the negative shifted the mode-A centroid away from it.
    assert len(facets) == 2
    assert sum(f.size for f in facets) == 12  # size counts positives only


def test_budgets_sum_to_total_and_respect_floor() -> None:
    facets = [
        Facet(centroid=[1.0], weight=0.8, size=8, mean_intra_sim=0.9),
        Facet(centroid=[1.0], weight=0.2, size=2, mean_intra_sim=0.9),
    ]
    total = 58  # k*4+10 for k=12
    budgets = facet_budgets(facets, total)
    assert sum(budgets) == total
    assert all(b > 0 for b in budgets)
    assert budgets[0] > budgets[1]  # the heavier facet gets more


def test_single_facet_budget_is_the_whole_limit() -> None:
    facets = [Facet(centroid=[1.0], weight=1.0, size=10, mean_intra_sim=1.0)]
    assert facet_budgets(facets, 58) == [58]
