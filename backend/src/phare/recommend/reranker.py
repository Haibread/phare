"""The deterministic re-ranker — where steering happens. Pure: no DB, no LLM, no clock.

Takes vector-similar candidates + the effective taste profile and produces an ordered slate:

1. **Score** = similarity x taste-affinity, minus a popularity penalty (anti-degeneracy: don't
   let blockbusters dominate) and a quality penalty (hold back poorly-rated titles).
2. **Diversity** = greedy MMR over genres so a slate isn't five of the same thing.
3. **Swing slots** = a reserved few high-novelty picks, deliberately *not* chosen for accuracy
   (design.md: discovery is the point; pure accuracy yields a popularity machine).

Every input maps to a stable output, so this is exhaustively unit-testable with synthetic data.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from phare.recommend import genres
from phare.recommend.schema import Candidate, Recommendation

# Scoring weights. Similarity leads; affinity steers; popularity is a mild penalty (a cap, not a
# boost — popularity must never be the thing that wins); quality gently demotes poorly-rated titles.
_W_SIMILARITY = 1.0
_W_AFFINITY = 0.6
_W_POPULARITY = 0.3
_W_QUALITY = 0.2
# Popularity at/above this counts as "blockbuster" and takes the full penalty.
_POPULARITY_CAP = 80.0
# TMDB mean rating below this takes a proportional quality penalty (0 at the floor, full at 0/10).
# A floor, not a boost: a well-rated title is never *rewarded*, a badly-rated one is just held back.
_QUALITY_FLOOR = 6.0
# How hard genre repetition is punished during MMR selection.
_DIVERSITY_LAMBDA = 0.5

# Per-query similarity normalization. Raw cosines from a general embedder are compressed near the
# top (a real 0.75–0.84 spread reads as a flat 0.87–0.92 after (sim+1)/2), so scoring and confidence
# off the absolute value make everything look equally good (review H2/A8). Instead, place each
# candidate *relative to its pool*: sim_rel = clamp01(0.5 + (sim - mean) / (SPREAD * std)). SPREAD=4
# maps ~±2σ onto [0,1], so the spread of a query's candidates fills the scale.
_SIM_REL_SPREAD = 4.0
# Confidence floor factor: a lightly-evidenced taste profile can't yield a blanket "strong fit".
# With taste confidence c, output confidence is capped at _CONF_FLOOR + (1 - _CONF_FLOOR) * c.
_CONF_FLOOR = 0.35
# A title with fewer than this many TMDB votes has no real quality signal yet (a just-released
# autoseed), so its displayed confidence is capped below the top buckets — it can be recommended as
# a discovery pick, never as a sure thing (review A9). Read at ranking time so a title that accrues
# votes graduates on its own. None (unknown) is *not* treated as unproven — only a known-low count.
_MIN_PROVEN_VOTES = 200
_UNPROVEN_CONF_CAP = 0.5

# Chat slate composition by vote count (how well-known a title is — TMDB rating count). This is a
# deliberate *mix*, not a popularity ranking: ~half well-known, ~a third lesser-known, ~15% low-vote
# for genuine discovery. The mix controls *which* titles make the slate; the slate is then ordered
# by score — relevance first, never by votes (which would bury the best match under the best-known).
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
    best-scored within each tier, backfilled when a tier is short, then ordered by score desc.

    The vote mix decides *membership* (a deliberate spread of known-ness); ordering is by score, so
    the most *relevant* pick leads — not the most-voted one (review A1: popularity was burying the
    best match at the bottom of the strip)."""
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
    chosen.sort(key=lambda item: item[0], reverse=True)  # rank by score (relevance), not votes
    return chosen[:k]


def _affinity_score(candidate: Candidate, affinities: Mapping[str, float]) -> float:
    """Net taste affinity for a candidate's genres/keywords, clamped to [-1, 1].

    An affinity key contributes its weight once if it matches *any* of the candidate's tokens under
    the shared genre-match rule (alias + substring), so free keys like "Sci-Fi" line up with a
    "Science Fiction" tag instead of scoring 0 on an exact-string miss (review H1)."""
    if not affinities:
        return 0.0
    tokens = [*candidate.genres, *candidate.keywords]
    if not tokens:
        return 0.0
    total = sum(
        float(weight) for key, weight in affinities.items() if genres.matches_any((key,), tokens)
    )
    return max(-1.0, min(1.0, total))


def _popularity_penalty(candidate: Candidate) -> float:
    """0 for a niche title, up to 1 for a blockbuster."""
    if candidate.popularity is None:
        return 0.0
    return min(max(candidate.popularity, 0.0), _POPULARITY_CAP) / _POPULARITY_CAP


def _quality_penalty(candidate: Candidate) -> float:
    """0 for a title rated at/above the floor, up to 1 for a 0/10. Unknown rating → 0 (no guess)."""
    if candidate.vote_average is None:
        return 0.0
    return max(0.0, (_QUALITY_FLOOR - candidate.vote_average) / _QUALITY_FLOOR)


def _relative_similarities(sims: Sequence[float]) -> list[float]:
    """Place each raw similarity relative to its pool: ``clamp01(0.5 + (sim-mean)/(SPREAD*std))``.

    Neutral fallback (0.5 for all) when there's nothing to normalise against: a pool smaller than 3,
    or a near-zero spread (every candidate equally similar). Pure — depends only on the pool passed.
    """
    n = len(sims)
    if n < 3:
        return [0.5] * n
    mean = sum(sims) / n
    std = (sum((s - mean) ** 2 for s in sims) / n) ** 0.5
    if std < 1e-6:
        return [0.5] * n
    return [max(0.0, min(1.0, 0.5 + (s - mean) / (_SIM_REL_SPREAD * std))) for s in sims]


def score_candidate(
    candidate: Candidate, taste: Mapping[str, Any], *, sim_rel: float | None = None
) -> tuple[float, dict[str, float]]:
    """Deterministic score + a transparent component breakdown.

    ``sim_rel`` is the candidate's pool-relative similarity (see :func:`_relative_similarities`); it
    is what scores, so a query's spread of candidates actually separates. When omitted (a lone
    candidate scored outside a pool) it falls back to the absolute normalised similarity.
    """
    sim_norm = (candidate.similarity + 1.0) / 2.0  # cosine [-1,1] -> [0,1], absolute
    sim_effective = sim_norm if sim_rel is None else sim_rel
    affinity = _affinity_score(candidate, taste.get("affinities", {}) or {})
    affinity_norm = (affinity + 1.0) / 2.0  # [-1,1] -> [0,1], 0.5 = neutral
    pop_penalty = _popularity_penalty(candidate)
    quality_penalty = _quality_penalty(candidate)
    score = (
        _W_SIMILARITY * sim_effective
        + _W_AFFINITY * affinity_norm
        - _W_POPULARITY * pop_penalty
        - _W_QUALITY * quality_penalty
    )
    components = {
        "similarity": round(sim_norm, 4),  # absolute (kept for existing readers of the breakdown)
        "similarity_rel": round(sim_effective, 4),  # pool-relative — this is what scores now
        "affinity": round(affinity_norm, 4),
        "popularity_penalty": round(pop_penalty, 4),
        "quality_penalty": round(quality_penalty, 4),
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


def _confidence(
    taste: Mapping[str, Any],
    *,
    is_swing: bool,
    sim_rel: float,
    affinity_norm: float,
    unproven: bool = False,
) -> float:
    """Honest confidence, blending the signals we actually have:

    - **relative similarity** — where this pick sits *within its pool*, not an absolute cosine
      (compressed near the top, so it reads "strong fit" for everything — review H2/A8);
    - **affinity** — does it hit a genre they like (the *steering* signal); folding this in is what
      stops a whole row collapsing to one label, since affinity varies title-to-title. Only counted
      when a taste profile exists — with none, similarity is all we honestly have, so we don't drag
      every pick toward neutral;
    - **taste confidence** — how much history backs the profile at all.

    A thin taste profile also *caps* the result: a barely-evidenced profile can't emit a blanket
    "strong fit" (A8). An ``unproven`` title (barely any votes — a just-dropped release) is likewise
    capped: recommending an unwatched-by-anyone title *with confidence* contradicts the whole point
    of the meter (review A9). Swings hedge low regardless — a reserved discovery pick is a bet.
    """
    signals = [sim_rel]
    if taste.get("affinities"):
        signals.append(affinity_norm)
    taste_conf = taste.get("confidence")
    if taste_conf is not None:
        signals.append(float(taste_conf))
    base = sum(signals) / len(signals)
    if taste_conf is not None:  # a lightly-evidenced profile can't claim high confidence
        base = min(base, _CONF_FLOOR + (1.0 - _CONF_FLOOR) * float(taste_conf))
    if unproven:  # no quality signal yet — can't be a confident pick
        base = min(base, _UNPROVEN_CONF_CAP)
    if is_swing:
        base *= 0.5
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
    vote count — ~50/35/15 well-known / lesser-known / low-vote — ordered by score, so the chat
    slate leads with the most *relevant* pick while still spanning a range of known-ness.
    """
    if not candidates:
        return []

    sim_rels = _relative_similarities([c.similarity for c in candidates])
    scored: list[tuple[float, Candidate, dict[str, float]]] = []
    for candidate, sim_rel in zip(candidates, sim_rels, strict=True):
        score, components = score_candidate(candidate, taste, sim_rel=sim_rel)
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
        confidence=_confidence(
            taste,
            is_swing=is_swing,
            sim_rel=components["similarity_rel"],
            affinity_norm=components["affinity"],
            unproven=candidate.vote_count is not None and candidate.vote_count < _MIN_PROVEN_VOTES,
        ),
        poster_path=candidate.poster_path,
        overview=candidate.overview,
        keywords=list(candidate.keywords),
        components=components,
    )
