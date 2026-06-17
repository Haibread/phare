"""Pure recommendation-quality metrics. No DB, no engine — operate on plain lists.

Anti-degeneracy metrics guard against "recommend the same blockbuster to everyone forever".
They are in deliberate tension with holdout accuracy; that tension is the product (design.md).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

# Popularity at/above this is treated as fully "popular" (matches the re-ranker's cap).
_POPULARITY_CAP = 80.0


def _norm_pop(popularity: float | None) -> float:
    if popularity is None:
        return 0.0
    return min(max(popularity, 0.0), _POPULARITY_CAP) / _POPULARITY_CAP


def popularity_bias(popularities: Sequence[float | None]) -> float:
    """Mean normalised popularity of a slate, in [0, 1]. High = leaning on blockbusters."""
    if not popularities:
        return 0.0
    return sum(_norm_pop(p) for p in popularities) / len(popularities)


def novelty(popularities: Sequence[float | None]) -> float:
    """Inverse of popularity bias — how off-the-beaten-path a slate is."""
    return 1.0 - popularity_bias(popularities)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 1.0


def intra_list_diversity(genre_lists: Sequence[Sequence[str]]) -> float:
    """1 - mean pairwise genre Jaccard. 0 = identical genres throughout, 1 = all distinct."""
    sets = [set(g) for g in genre_lists]
    if len(sets) < 2:
        return 1.0
    sims: list[float] = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            sims.append(_jaccard(sets[i], sets[j]))
    return 1.0 - (sum(sims) / len(sims))


def catalog_coverage(recommended_ids: Sequence[uuid.UUID], catalog_size: int) -> float:
    """Fraction of the catalog reachable across (possibly many) slates."""
    if catalog_size <= 0:
        return 0.0
    return len(set(recommended_ids)) / catalog_size


def recall_at_k(recommended_ids: Sequence[uuid.UUID], held_out_ids: Sequence[uuid.UUID]) -> float:
    """Temporal-holdout recall: fraction of held-out titles that surfaced in the slate."""
    held = set(held_out_ids)
    if not held:
        return 0.0
    return len(set(recommended_ids) & held) / len(held)
