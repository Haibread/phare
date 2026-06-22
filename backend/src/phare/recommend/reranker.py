"""The deterministic re-ranker — where steering happens. Pure: no DB, no LLM, no clock.

Takes vector-similar candidates + the effective taste profile and produces an ordered slate:

1. **Score** = similarity x taste-affinity, minus a popularity penalty (anti-degeneracy: don't
   let blockbusters dominate).
2. **Diversity** = greedy MMR over genres so a slate isn't five of the same thing.
3. **Swing slots** = a reserved few high-novelty picks, deliberately *not* chosen for accuracy
   (design.md: discovery is the point; pure accuracy yields a popularity machine).

Every input maps to a stable output, so this is exhaustively unit-testable with synthetic data.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from phare.recommend.schema import Candidate, Recommendation

# Scoring weights. Similarity leads; affinity steers; popularity is a mild penalty (a cap, not a
# boost — popularity must never be the thing that wins).
_W_SIMILARITY = 1.0
_W_AFFINITY = 0.6
_W_POPULARITY = 0.3
# Popularity at/above this counts as "blockbuster" and takes the full penalty.
_POPULARITY_CAP = 80.0
# How hard genre repetition is punished during MMR selection.
_DIVERSITY_LAMBDA = 0.5

# Chat slate composition by vote count (how well-known a title is — TMDB rating count). This is a
# deliberate *mix*, not a popularity ranking: ~half well-known, ~a third lesser-known, ~15% low-vote
# for genuine discovery. The slate is then ordered most-voted-first (the chat UI's "rank by votes").
_POPULAR_VOTE_FLOOR = 2000  # at/above this a title is "well-known"
_LOWVOTE_CEILING = 300  # below this it's "low-vote / discovery"
_VOTE_MIX = (0.50, 0.35, 0.15)  # well-known, lesser-known, low-vote


def _vote_tier(candidate: Candidate) -> int:
    """0 = well-known, 1 = lesser-known, 2 = low-vote. Unknown vote counts read as low-vote."""
    votes = candidate.vote_count or 0
    if votes >= _POPULAR_VOTE_FLOOR:
        return 0
    if votes < _LOWVOTE_CEILING:
        return 2
    return 1


def _tier_targets(k: int) -> list[int]:
    """Per-tier slot counts summing to ``k``, from ``_VOTE_MIX`` (largest-remainder rounding)."""
    raw = [k * frac for frac in _VOTE_MIX]
    targets = [int(x) for x in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - targets[i], reverse=True)
    for i in range(k - sum(targets)):  # hand out the leftover slots by largest remainder
        targets[order[i % len(order)]] += 1
    return targets


def _select_vote_mix(
    scored: list[tuple[float, Candidate, dict[str, float]]], k: int
) -> list[tuple[float, Candidate, dict[str, float]]]:
    """Compose a slate of up to ``k`` as the ``_VOTE_MIX`` of well-known / lesser-known / low-vote,
    best-scored within each tier, backfilled when a tier is short, then ordered by vote count."""
    buckets: dict[int, list[tuple[float, Candidate, dict[str, float]]]] = {0: [], 1: [], 2: []}
    for item in scored:  # scored is already sorted by score desc, so each bucket is too
        buckets[_vote_tier(item[1])].append(item)
    targets = _tier_targets(k)
    chosen: list[tuple[float, Candidate, dict[str, float]]] = []
    for tier in (0, 1, 2):
        chosen.extend(buckets[tier][: targets[tier]])
    if len(chosen) < k:  # a tier ran short (thin catalog) — backfill with the next best-scored
        chosen_ids = {c.title_id for _, c, _ in chosen}
        for item in scored:
            if item[1].title_id not in chosen_ids:
                chosen.append(item)
                chosen_ids.add(item[1].title_id)
                if len(chosen) >= k:
                    break
    chosen.sort(key=lambda item: item[1].vote_count or 0, reverse=True)  # rank by votes
    return chosen[:k]


def _affinity_score(candidate: Candidate, affinities: Mapping[str, float]) -> float:
    """Net taste affinity for a candidate's genres/keywords, clamped to [-1, 1]."""
    if not affinities:
        return 0.0
    lowered = {key.lower(): float(value) for key, value in affinities.items()}
    total = 0.0
    for token in (*candidate.genres, *candidate.keywords):
        total += lowered.get(token.lower(), 0.0)
    return max(-1.0, min(1.0, total))


def _popularity_penalty(candidate: Candidate) -> float:
    """0 for a niche title, up to 1 for a blockbuster."""
    if candidate.popularity is None:
        return 0.0
    return min(max(candidate.popularity, 0.0), _POPULARITY_CAP) / _POPULARITY_CAP


def score_candidate(
    candidate: Candidate, taste: Mapping[str, Any]
) -> tuple[float, dict[str, float]]:
    """Deterministic score + a transparent component breakdown."""
    sim_norm = (candidate.similarity + 1.0) / 2.0  # cosine [-1,1] -> [0,1]
    affinity = _affinity_score(candidate, taste.get("affinities", {}) or {})
    affinity_norm = (affinity + 1.0) / 2.0  # [-1,1] -> [0,1], 0.5 = neutral
    pop_penalty = _popularity_penalty(candidate)
    score = _W_SIMILARITY * sim_norm + _W_AFFINITY * affinity_norm - _W_POPULARITY * pop_penalty
    components = {
        "similarity": round(sim_norm, 4),
        "affinity": round(affinity_norm, 4),
        "popularity_penalty": round(pop_penalty, 4),
        "score": round(score, 4),
    }
    return score, components


def _genre_overlap(candidate: Candidate, covered: Counter[str]) -> float:
    """Fraction of a candidate's genres already represented in the slate (0..1)."""
    if not candidate.genres:
        return 0.0
    hit = sum(1 for g in candidate.genres if covered[g] > 0)
    return hit / len(candidate.genres)


def _select_diverse(
    scored: list[tuple[float, Candidate, dict[str, float]]], count: int
) -> list[tuple[float, Candidate, dict[str, float]]]:
    """Greedy MMR: repeatedly take the highest score-minus-genre-overlap candidate."""
    chosen: list[tuple[float, Candidate, dict[str, float]]] = []
    covered: Counter[str] = Counter()
    pool = list(scored)
    while pool and len(chosen) < count:
        best_idx = max(
            range(len(pool)),
            key=lambda i: pool[i][0] - _DIVERSITY_LAMBDA * _genre_overlap(pool[i][1], covered),
        )
        score, candidate, components = pool.pop(best_idx)
        chosen.append((score, candidate, components))
        covered.update(candidate.genres)
    return chosen


def _confidence(candidate: Candidate, taste: Mapping[str, Any], *, is_swing: bool) -> float:
    """Honest confidence: blend taste confidence with this title's similarity; swings hedge low."""
    sim_norm = (candidate.similarity + 1.0) / 2.0
    taste_conf = taste.get("confidence")
    base = sim_norm if taste_conf is None else (sim_norm + float(taste_conf)) / 2.0
    if is_swing:
        base *= 0.5  # a deliberate gamble — say so
    return round(max(0.0, min(1.0, base)), 3)


def rerank(
    candidates: Sequence[Candidate],
    taste: Mapping[str, Any],
    *,
    k: int = 12,
    swing_slots: int = 2,
    vote_mix: bool = False,
) -> list[Recommendation]:
    """Order candidates into a slate of up to ``k``, reserving ``swing_slots`` novelty picks.

    ``vote_mix=True`` (the chat path) ignores swing slots and instead composes a deliberate mix by
    vote count — ~50/35/15 well-known / lesser-known / low-vote — ordered most-voted-first, so the
    chat slate reads like a sensible "best known first" list with a discovery tail.
    """
    if not candidates:
        return []

    scored: list[tuple[float, Candidate, dict[str, float]]] = []
    for candidate in candidates:
        score, components = score_candidate(candidate, taste)
        scored.append((score, candidate, components))
    scored.sort(key=lambda item: item[0], reverse=True)

    if vote_mix:
        return [
            _to_rec(candidate, score, components, taste, is_swing=False)
            for score, candidate, components in _select_vote_mix(scored, k)
        ]

    swing_slots = max(0, min(swing_slots, k))
    main_slots = max(0, k - swing_slots)

    main = _select_diverse(scored, main_slots)
    chosen_ids = {c.title_id for _, c, _ in main}

    # Swings: the most *novel* leftovers (lowest similarity), not the next-best by score.
    leftovers = [item for item in scored if item[1].title_id not in chosen_ids]
    leftovers.sort(key=lambda item: item[1].similarity)  # ascending = most novel first
    swings = leftovers[:swing_slots]

    recommendations: list[Recommendation] = []
    for score, candidate, components in main:
        recommendations.append(_to_rec(candidate, score, components, taste, is_swing=False))
    for score, candidate, components in swings:
        recommendations.append(_to_rec(candidate, score, components, taste, is_swing=True))
    return recommendations


def _to_rec(
    candidate: Candidate,
    score: float,
    components: dict[str, float],
    taste: Mapping[str, Any],
    *,
    is_swing: bool,
) -> Recommendation:
    return Recommendation(
        title_id=candidate.title_id,
        title=candidate.title,
        kind=candidate.kind,
        year=candidate.year,
        genres=candidate.genres,
        score=round(score, 4),
        is_swing=is_swing,
        confidence=_confidence(candidate, taste, is_swing=is_swing),
        poster_path=candidate.poster_path,
        components=components,
    )
