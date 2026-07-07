"""The deterministic re-ranker — where steering happens. No DB, no LLM, no clock (it may emit a
:func:`record_fallback` observability signal when it trims a slate, but reads no external state).

Takes vector-similar candidates + the effective taste profile and produces an ordered slate:

1. **Score** = similarity x taste-affinity, minus a popularity penalty (anti-degeneracy: don't
   let blockbusters dominate) and a quality penalty (hold back poorly-rated titles).
2. **Diversity** = greedy MMR over genres so a slate isn't five of the same thing.
3. **Swing slots** = a reserved few high-novelty picks, deliberately *not* chosen for accuracy
   (design.md: discovery is the point; pure accuracy yields a popularity machine).

Every input maps to a stable output, so this is exhaustively unit-testable with synthetic data.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from phare.core.fallback import record_fallback
from phare.recommend import genres
from phare.recommend.schema import Candidate, Recommendation

# Scoring weights. Similarity leads; affinity steers; popularity is a mild penalty (a cap, not a
# boost — popularity must never be the thing that wins); quality gently demotes poorly-rated titles.
_W_SIMILARITY = 1.0
# Affinity now *grades* (review R1 — it used to saturate at 1.0 for nearly every candidate, adding a
# constant 0.6 offset that steered nothing). Graded, ``affinity_norm`` spreads across ~[0.5, 0.95]
# for real profiles, so at 0.6 it can swing a score by up to ~0.27 — enough to reorder titles of
# comparable similarity (that's steering) without overriding the pool-relative similarity that
# leads. Kept at 0.6: the weight was never the saturation bug, the score was, so re-tuning it would
# only muddy the before/after comparison.
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

# Confidence blend weights (lot R2 — "the fit badge must discriminate"). Confidence is the input to
# the frontend fit chip and the eval anti-uniformity guardrail; it must *spread* across a real
# slate, not read "strong fit" for everything. Measured live on the R1-merged branch, the old mean
# of three terms — pool-relative similarity, affinity, and taste-confidence — put 35/38 and 41/42
# displayed items into the top bucket. Two structural reasons, addressed per term below:
#
#   sim_rel (0.55): pool-relative placement is the sharpest per-title taste signal now that R1 made
#   affinity genuinely spread; it stays the lead term. But "top of its pool" alone can't tell a
#   strong pool from a weak one (every query has a #1), so it no longer carries the whole weight.
#
#   affinity (0.25): the *steering* signal — does the pick hit a genre they like. It varies
#   title-to-title, so folding it in is what keeps a row from collapsing to one label. Only counted
#   when a taste profile exists; with none, similarity is all we honestly have (below).
#
#   sim_abs pool-strength (0.20): absolute normalised similarity, rescaled against the embedder's
#   observed compressed band (``_SIM_ABS_*`` below). This is the term that makes "top of a weak
#   pool" read lower than "top of a strong pool" — sim_rel is 1.0 for the #1 of *any* pool, but its
#   absolute cosine still says how good that #1 actually is. Without it, a thin-taste user's best-of
#   a-mediocre-set reads as confidently as a rich-taste user's genuine bullseye.
#
# The former third term, taste-confidence, is deliberately *dropped from the mean*: it's a
# per-profile constant (~0.9 on both live profiles), so averaging it into every card lifted the
# whole slate equally while informing nothing per-title — pure spread compression. It survives only
# as the CAP it already provided (``_CONF_FLOOR`` below): a thin history still can't emit a blanket
# "strong fit", but it no longer inflates a well-evidenced profile's every card toward that label.
_W_CONF_SIM_REL = 0.55
_W_CONF_AFFINITY = 0.25
_W_CONF_SIM_ABS = 0.20
# Absolute-similarity pool-strength band. Raw cosines from the production embedder land compressed
# in roughly [0.76, 0.92] once mapped to [0,1] (measured live: profile A 0.79–0.92, B 0.76–0.88).
# Rescale that band to [0,1] so the *level* of a pool separates a strong match from a merely-least-
# bad one; below the floor reads ~0, at/above the ceiling reads ~1. A generous floor of 0.70 keeps a
# genuinely weak absolute match from being clamped straight to 0 on a single noisy query.
_SIM_ABS_FLOOR = 0.70
_SIM_ABS_CEIL = 0.92

# Fit-chip bucket thresholds — the CANONICAL source, mirrored by the frontend ``fitFor`` in
# ``frontend/src/lib/fit.ts`` (keep the two in sync; the numbers must match). Confidence at/above
# ``_FIT_STRONG`` reads "strong fit", at/above ``_FIT_TRY`` reads "worth a try", below is "long
# shot" (plus swings, which get their own "a stretch" regardless). Calibrated against the new
# confidence blend on the two live profiles: with these cuts the displayed slate lands in ≥2 buckets
# with no bucket above ~60% (A 5%/55%/39%, B 0%/60%/40% long/try/strong) — a badge that's constant
# is not information (lot R2). The bar was lifted from the old 0.66/0.40 because the recalibrated
# blend rightly produces high values for genuinely strong picks; "strong" now means a real top pick,
# not merely above the pool median. The eval anti-uniformity guardrail reads these same constants.
_FIT_STRONG = 0.72
_FIT_TRY = 0.45

# Chat slate composition by vote count (how well-known a title is — TMDB rating count). This is a
# deliberate *mix*, not a popularity ranking: ~half well-known, ~a third lesser-known, ~15% low-vote
# for genuine discovery. The mix controls *which* titles make the slate; the slate is then ordered
# by score — relevance first, never by votes (which would bury the best match under the best-known).
_POPULAR_VOTE_FLOOR = 2000  # at/above this a title is "well-known"
_LOWVOTE_CEILING = 300  # below this it's "low-vote / discovery"
_VOTE_MIX = (0.50, 0.35, 0.15)  # well-known, lesser-known, low-vote

# Chat-slate relevance floor (honesty over engagement, principle 4). The chat path (``vote_mix``)
# used to *pad* the slate up to ``k`` from the candidate pool regardless of fit, so a request like
# "a feel-good comedy" ended with titles at pool-relative similarity ~0.27 — a genuinely weak match
# shown only to reach 12. Instead, drop candidates whose pool-relative similarity is below this
# floor and return a shorter, honest slate. Measured on ``similarity_rel`` (where a pick sits
# *within its pool*), the same relative scale the confidence meter and the eval spread check use. It
# only bites when the pool is large enough to normalise against (n>=3, real spread) — with a flat
# pool every ``sim_rel`` is the neutral 0.5 and nothing is dropped, so we never trim what we can't
# measure. A trimmed slate is surfaced via ``record_fallback`` so the erosion is never silent (G1).
_CHAT_SIM_REL_FLOOR = 0.35


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


def _relevant_enough(item: tuple[float, Candidate, dict[str, float]]) -> bool:
    """Whether a scored candidate clears the chat-slate relevance floor (``_CHAT_SIM_REL_FLOOR``).

    Measured on the pool-relative ``similarity_rel`` — the same scale the confidence meter reads —
    so the floor means "not among the weakest fits in this pool", not an absolute cosine. When the
    pool is too small/flat to normalise, ``_relative_similarities`` hands back the neutral 0.5 for
    everyone, which clears the floor: we never trim a slate we couldn't measure the spread of.
    """
    return item[2].get("similarity_rel", 0.5) >= _CHAT_SIM_REL_FLOOR


def _select_vote_mix(
    scored: list[tuple[float, Candidate, dict[str, float]]], k: int
) -> list[tuple[float, Candidate, dict[str, float]]]:
    """Compose a slate of up to ``k`` as the ``_VOTE_MIX`` of well-known / lesser-known / low-vote,
    best-scored within each tier, backfilled when a tier is short, then ordered by score desc.

    The vote mix decides *membership* (a deliberate spread of known-ness); ordering is by score, so
    the most *relevant* pick leads — not the most-voted one (review A1: popularity was burying the
    best match at the bottom of the strip).

    Candidates below the relevance floor (:func:`_relevant_enough`) are dropped *before* the
    composition, so a thin pool yields a shorter, honest slate rather than one padded with weak fits
    (principle 4). The caller records the trim as a fallback so it's never silent."""
    scored = [item for item in scored if _relevant_enough(item)]
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
    """Net *graded* taste affinity for a candidate's genres/keywords, in [-1, 1].

    Old semantics (review R1) summed the *raw* weights of matched keys and clamped to [-1, 1]. A
    real profile carries a dozen-plus affinities weighted 0.3–0.9, so matching just two positives
    (e.g. Action 0.9 + Crime 0.8 = 1.7) already clamped to 1.0 — every candidate that hit any two
    liked genres scored a flat 1.0. The component stopped discriminating: a stoner comedy tagged
    "Science Fiction" read the same as a three-way thriller match, and the confidence meter it feeds
    collapsed to "Forte affinité" for the whole slate.

    Graded semantics: score matches by *how much taste weight they satisfy*, against a saturating
    budget, so breadth and weight both count and the result stays graded instead of saturating.

    - Split the affinities into positives (weight > 0) and negatives (weight < 0).
    - ``pos = sum(matched positive weights) / budget(positive weights)`` — the share of the taste's
      positive "pull" this candidate satisfies. The budget is the sum of the ``_AFFINITY_BUDGET_K``
      *strongest* positive weights, not the whole profile: a candidate carries only a handful of
      genres/keywords, so it can realistically hit only a few affinities, and normalising against
      the full dozen-plus-key budget would crush every real match to a sliver (a 3-strong-match
      would read barely above neutral). Capping the denominator at the top-K keeps fractions honest
      *and* spread: matching three strong positives fills most of the budget → high; matching a
      single mid-weight key → a small fraction; and the ``min(1.0, …)`` clamp handles a candidate
      that happens to match more than K keys.
    - ``neg = sum(|matched negative weights|) / budget(negative weights)`` — same construction for
      dislikes; it pulls the net *down* proportionally. (Hard-avoids are a separate upstream filter;
      this only handles soft negative taste.)
    - ``net = pos - neg``, in [-1, 1] since each share is clamped to [0, 1].

    A candidate that matches nothing scores 0 (→ neutral 0.5 after normalisation): vocabulary
    silence is never punished (principle 4, honesty). Deterministic, no I/O, no LLM. Free keys
    still line up with catalog tags via the shared matcher, so "Sci-Fi" hits "Science Fiction" (H1).
    """
    if not affinities:
        return 0.0
    tokens = [*candidate.genres, *candidate.keywords]
    if not tokens:
        return 0.0
    pos_matched = [
        float(w)
        for k, w in affinities.items()
        if float(w) > 0.0 and genres.matches_any((k,), tokens)
    ]
    neg_matched = [
        -float(w)
        for k, w in affinities.items()
        if float(w) < 0.0 and genres.matches_any((k,), tokens)
    ]
    pos = min(1.0, sum(pos_matched) / _affinity_budget(affinities, positive=True))
    neg = min(1.0, sum(neg_matched) / _affinity_budget(affinities, positive=False))
    return max(-1.0, min(1.0, pos - neg))


# How many of the strongest same-sign weights form the normalisation budget. A candidate carries a
# few genres plus a few keywords, so it can realistically satisfy on the order of this many affinity
# keys; capping the denominator here (rather than at the whole profile) is what keeps a genuine
# 3-strong match well above a 1-mid match instead of both reading near-neutral (review R1). Set to
# 4 to match the typical 2–4 genres a title is tagged with; larger only flattens the gradient again.
_AFFINITY_BUDGET_K = 4


def _affinity_budget(affinities: Mapping[str, float], *, positive: bool) -> float:
    """Sum of the ``_AFFINITY_BUDGET_K`` strongest same-sign magnitudes (empty → 1.0, no /0)."""
    magnitudes = sorted(
        (abs(float(w)) for w in affinities.values() if (float(w) > 0.0) == positive),
        reverse=True,
    )
    total = sum(magnitudes[:_AFFINITY_BUDGET_K])
    return total if total > 0.0 else 1.0


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


def _raw_similarity(candidate: Candidate) -> float:
    """The candidate's honest raw cosine. Facet-merged candidates carry it on ``raw_similarity``
    (their ``similarity`` is the facet-relative placement); single-vector candidates carry it on
    ``similarity`` itself. Everything that must read the *true* scale — the confidence blend's
    absolute band, swing novelty — goes through here."""
    return (
        candidate.raw_similarity if candidate.raw_similarity is not None else candidate.similarity
    )


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
    # The absolute reading must be the TRUE cosine: a facet-merged candidate's ``similarity`` is
    # its facet-relative placement (see service._merge_facet_pools), the right thing to *rank* by
    # but a lie to the confidence blend's absolute band. ``raw_similarity`` carries the honest
    # value there; on single-vector paths it is None and ``similarity`` is already the raw cosine.
    sim_norm = (_raw_similarity(candidate) + 1.0) / 2.0  # cosine [-1,1] -> [0,1], absolute
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
    if candidate.facet is not None:  # transparency: which taste facet surfaced this pick
        components["facet"] = float(candidate.facet)
    return score, components


def _genre_overlap(candidate: Candidate, covered: Counter[str]) -> float:
    """Fraction of a candidate's genres already represented in the slate (0..1)."""
    if not candidate.genres:
        return 0.0
    hit = sum(1 for g in candidate.genres if covered[g] > 0)
    return hit / len(candidate.genres)


# Facet-share guarantee (round 10, live finding). A profile whose taste splits 0.37/0.25/0.20/0.18
# across four facets must not render a 10/0/0/0 slate: every facet carrying at least this share of
# the taste mass is guaranteed at least one main slot (and its proportional share, floored), unless
# its candidates genuinely ran out post-filter — in which case the shortfall is recorded, never
# silent. Mirrors how swing slots are reserved: membership guarantees, score/MMR still orders.
_FACET_QUOTA_MIN_WEIGHT = 0.15


def _facet_quotas(
    facet_weights: Sequence[float] | None,
    scored: list[tuple[float, Candidate, dict[str, float]]],
    main_slots: int,
) -> dict[int, int] | None:
    """Reserved main-slate slots per facet: proportional to facet weight (``int(w * slots)``), with
    a floor of one slot for any facet at/above ``_FACET_QUOTA_MIN_WEIGHT``. The remainder is filled
    by global MMR. ``None`` (no reservation) when there's no facet structure to honour."""
    if not facet_weights or main_slots <= 0:
        return None
    if not any(candidate.facet is not None for _, candidate, _ in scored):
        return None
    quotas: dict[int, int] = {}
    for idx, weight in enumerate(facet_weights):
        reserved = int(weight * main_slots)
        if weight >= _FACET_QUOTA_MIN_WEIGHT:
            reserved = max(reserved, 1)
        if reserved > 0:
            quotas[idx] = reserved
    # The floors can only pathologically push the total past the slot count (many tiny facets);
    # trim from the lightest facet, deterministically, so reservations never exceed the slate.
    while sum(quotas.values()) > main_slots:
        lightest = min(quotas, key=lambda i: (facet_weights[i], -i))
        quotas[lightest] -= 1
        if quotas[lightest] == 0:
            del quotas[lightest]
    return quotas or None


# Franchise de-duplication (round-14 live finding: a chat slate carried both "Rush Hour 2" and
# "Rush Hour 3" — two instalments of one franchise is a wasted slot MMR can't see, since sequels sit
# close in embedding space but not close enough to be squashed by genre diversity). There is no
# franchise id in the data, so the key is approximated from the title. It must be *conservative*:
# merging two distinct works (dropping a good rec) is worse than missing a real sequel (a cosmetic
# dup), so the derivation errs toward None.
#
# Trailing tokens stripped when deriving the key — instalment markers, not part of the franchise
# name: roman numerals in the realistic sequel range, and the words that introduce a number
# ("Part 2", "Vol. 2", "Chapter 4").
_SEQUEL_ROMAN = frozenset(
    {"ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii"}
)
_SEQUEL_WORDS = frozenset({"part", "chapter", "vol", "volume"})
# A trailing arabic numeral only reads as a sequel index up to here (mirrors the roman set's xiii).
# Above it the number is almost always meaningful, not an instalment — a year ("Blade Runner 2049"),
# a synthetic id ("Title 600"), or a title number ("Apollo 13" strips, "Apollo 18" does not, so the
# two never merge; "Fahrenheit 451", "1917" stay whole). Under-merging is the safe direction.
_MAX_ARABIC_SEQUEL = 13


def _is_sequel_marker(token: str) -> bool:
    """Whether a trailing token is an instalment marker rather than part of the franchise name."""
    if token in _SEQUEL_ROMAN or token in _SEQUEL_WORDS:
        return True
    return token.isdigit() and int(token) <= _MAX_ARABIC_SEQUEL


# A lone single word shorter than this is too generic to stand as a franchise ("It", "Up", "Her") —
# only multi-word keys or *distinctive* single words (length ≥ this) dedupe. Chosen so "It" (2) can
# never merge with "It Follows" (two words, keys separately as ``it follows``) while "Alien"/"Jaws"
# still collapse their instalments — the tricky cases the guard is specced against.
_MIN_SINGLE_WORD_FRANCHISE_LEN = 4
# Strip leading/trailing punctuation off a token (so "Vol." → "vol") while keeping it intact inside.
_TOKEN_EDGE_PUNCT = re.compile(r"^\W+|\W+$")


def _franchise_key(candidate: Candidate) -> str | None:
    """A conservative franchise identity for a title, or ``None`` when it isn't safely one.

    Drop any subtitle (everything after the first ``:`` or a spaced `` - ``), then strip trailing
    instalment markers (arabic numerals — "Rush Hour *3*"; roman numerals — "Rocky *II*"; and the
    words that precede a number — "*Part* 2", "*Vol* 2"). Two titles share a franchise iff they have
    the same ``kind`` and the same key. Deliberately conservative — it must never merge two
    *distinct* works:

    - a single-word key must be *distinctive* (length ≥ ``_MIN_SINGLE_WORD_FRANCHISE_LEN``) to
      count, so "It" (2 chars) is never a franchise and cannot swallow "It Follows" (two words →
      keys separately as ``it follows``);
    - "Alien"/"Aliens" *should* merge, so a distinctive single-word key is folded to its singular
      (a trailing ``s`` dropped only when the stem stays ≥ 4 — which keeps "Cars"→"cars" from
      collapsing onto a hypothetical "Car").

    Returns the normalised key, or ``None`` when the title yields nothing franchise-worthy — a
    ``None`` key never dedupes, so unknowns are simply left alone (honest under-merging)."""
    title = candidate.title.casefold()
    for sep in (":", " - "):
        cut = title.find(sep)
        if cut != -1:
            title = title[:cut]
    tokens = [t for t in (_TOKEN_EDGE_PUNCT.sub("", tok) for tok in title.split()) if t]
    while tokens and _is_sequel_marker(tokens[-1]):
        tokens.pop()
    if not tokens:
        return None
    if len(tokens) == 1:
        stem = tokens[0]
        if stem.endswith("s") and len(stem) - 1 >= _MIN_SINGLE_WORD_FRANCHISE_LEN:
            stem = stem[:-1]
        if len(stem) < _MIN_SINGLE_WORD_FRANCHISE_LEN:
            return None
        return stem
    return " ".join(tokens)


def _dedupe_franchises(
    scored: list[tuple[float, Candidate, dict[str, float]]],
) -> list[tuple[float, Candidate, dict[str, float]]]:
    """Keep at most one title per ``(franchise, kind)`` — the best-scored one.

    ``scored`` must already be ordered by score descending, so the first candidate seen for a
    franchise is its strongest representative. Applied *before* any slate is composed, so it
    protects every path uniformly: the row path (MMR), the chat path (vote-mix — where the live
    "Rush Hour 2 + Rush Hour 3" dup was actually seen, and which never touches
    :func:`_select_diverse`), and swings (their leftover pool is drawn from the same deduped set).
    Candidates with no franchise key (``None``) are always kept — an unknown never merges."""
    seen: set[tuple[str, str]] = set()
    kept: list[tuple[float, Candidate, dict[str, float]]] = []
    for item in scored:
        key = _franchise_key(item[1])
        if key is not None:
            ident = (key, item[1].kind)
            if ident in seen:
                continue
            seen.add(ident)
        kept.append(item)
    return kept


def _select_diverse(
    scored: list[tuple[float, Candidate, dict[str, float]]],
    count: int,
    *,
    quotas: dict[int, int] | None = None,
) -> list[tuple[float, Candidate, dict[str, float]]]:
    """Greedy MMR: repeatedly take the highest score-minus-genre-overlap candidate.

    With ``quotas`` (facet index → reserved slots), the greedy pick is constrained just enough to
    honour them: while the remaining slots exceed the unmet reservations, selection is free (best
    MMR pick wins, whatever its facet); once the remaining slots are all spoken for, only candidates
    from under-quota facets are eligible. MMR still orders *within* the constraint, so diversity and
    score behave as before — the quota only decides membership, exactly like swing slots. A facet
    whose candidates run out before its reservation is met is released (and recorded via
    ``record_fallback``) so the slate still fills — filters emptying a facet is honest, hiding three
    facets behind one is not."""
    chosen: list[tuple[float, Candidate, dict[str, float]]] = []
    covered: Counter[str] = Counter()
    pool = list(scored)
    unmet: dict[int, int] = dict(quotas or {})
    while pool and len(chosen) < count:
        if unmet:
            # Release reservations no candidate can satisfy any more (the facet ran out post-
            # filter) — visible, never a deadlock.
            available = {c.facet for _, c, _ in pool}
            starved = [idx for idx in unmet if idx not in available]
            for idx in starved:
                record_fallback("reranker", "facet_quota_starved", facet=idx, unmet=unmet[idx])
                del unmet[idx]
        remaining = count - len(chosen)
        deficit = sum(unmet.values())
        if unmet and deficit >= remaining:
            eligible = [i for i in range(len(pool)) if unmet.get(pool[i][1].facet, 0) > 0]
        else:
            eligible = list(range(len(pool)))
        if not eligible:
            eligible = list(range(len(pool)))
        best_idx = max(
            eligible,
            key=lambda i: pool[i][0] - _DIVERSITY_LAMBDA * _genre_overlap(pool[i][1], covered),
        )
        score, candidate, components = pool.pop(best_idx)
        chosen.append((score, candidate, components))
        covered.update(candidate.genres)
        facet = candidate.facet
        if facet is not None and unmet.get(facet, 0) > 0:
            unmet[facet] -= 1
            if unmet[facet] == 0:
                del unmet[facet]
    return chosen


def _sim_abs_strength(sim_norm: float) -> float:
    """Rescale absolute normalised similarity to a [0,1] pool-strength reading over the embedder's
    observed compressed band (``_SIM_ABS_FLOOR``..``_SIM_ABS_CEIL``). Answers "how good is this pick
    *in absolute terms*", the signal ``sim_rel`` (relative to its pool) can't carry — the #1 of a
    weak pool and the #1 of a strong one both read sim_rel≈1.0, but their absolute cosines differ.
    """
    return max(0.0, min(1.0, (sim_norm - _SIM_ABS_FLOOR) / (_SIM_ABS_CEIL - _SIM_ABS_FLOOR)))


def _confidence(
    taste: Mapping[str, Any],
    *,
    is_swing: bool,
    sim_rel: float,
    sim_norm: float,
    affinity_norm: float,
    unproven: bool = False,
) -> float:
    """Honest, *discriminating* confidence (lot R2) — a weighted blend of the per-title signals that
    actually vary across a slate, so the fit chip spreads instead of reading "strong fit" for
    everything (the owner's complaint: every home-row card at 3/3). The former equal-weight mean of
    three terms compressed the range; the weights and rationale live on ``_W_CONF_*`` above.

    - **pool-relative similarity** (``_W_CONF_SIM_REL``) — where this pick sits *within its pool*,
      the sharpest per-title taste signal; the lead term.
    - **absolute pool-strength** (``_W_CONF_SIM_ABS``) — how good the match is in absolute terms, so
      "top of a weak pool" reads lower than "top of a strong pool" (``sim_rel`` alone can't, since
      every pool has a #1). See :func:`_sim_abs_strength`.
    - **affinity** (``_W_CONF_AFFINITY``) — does it hit a genre they like (the *steering* signal);
      varies title-to-title, so it keeps a row from collapsing to one label. Only counted when a
      taste profile exists — with none, similarity is all we honestly have.

    When there is **no taste profile**, affinity carries no signal, so it's dropped and the two
    similarity terms are renormalised to sum to 1 — the no-taste path stays purely similarity-driven
    (relative placement + absolute strength), never dragged toward a neutral affinity.

    **Taste-confidence is no longer in the mean** — a per-profile constant averaged into every card
    lifted the whole slate equally while informing nothing per-title (pure spread compression). It
    survives only as the CAP it already provided: a lightly-evidenced profile still can't emit a
    blanket "strong fit" (A8), but a well-evidenced one no longer has every card inflated toward it.

    An ``unproven`` title (barely any votes — a just-dropped release) is likewise capped:
    recommending an unwatched-by-anyone title *with confidence* contradicts the meter (A9). Swings
    hedge low regardless — a reserved discovery pick is a bet.
    """
    sim_abs = _sim_abs_strength(sim_norm)
    if taste.get("affinities"):
        base = (
            _W_CONF_SIM_REL * sim_rel + _W_CONF_SIM_ABS * sim_abs + _W_CONF_AFFINITY * affinity_norm
        )
    else:  # no steering signal — renormalise the two similarity terms to a full [0,1] blend
        sim_weight = _W_CONF_SIM_REL + _W_CONF_SIM_ABS
        base = (_W_CONF_SIM_REL * sim_rel + _W_CONF_SIM_ABS * sim_abs) / sim_weight
    taste_conf = taste.get("confidence")
    if taste_conf is not None:  # a lightly-evidenced profile can't claim high confidence
        base = min(base, _CONF_FLOOR + (1.0 - _CONF_FLOOR) * float(taste_conf))
    if unproven:  # no quality signal yet — can't be a confident pick
        base = min(base, _UNPROVEN_CONF_CAP)
    if is_swing:
        base *= 0.5
    return round(max(0.0, min(1.0, base)), 3)


def confidence_for_pool(
    candidates: Sequence[Candidate], taste: Mapping[str, Any]
) -> list[float | None]:
    """Honest per-title fit confidence for an *externally-ordered* pool — same blend the re-ranker
    stamps on a ranked slate, without re-ordering.

    The ``popular`` row is selected and ordered by popularity (its identity), but its fit gauge must
    read real taste, not popularity magnitude (lot R6b — the owner's "populaire n'a pas de rating").
    So it scores its own titles here: each candidate's cosine similarity to the taste centroid is
    placed pool-relative (:func:`_relative_similarities`) exactly as ``rerank`` does, folded with
    the graded affinity into the canonical :func:`_confidence` blend — including the ``unproven``
    cap, so a fresh, few-votes release can't read as a confident pick (popular skews recent). No
    swing hedging: these aren't reserved discovery slots.

    Returns one confidence in ``[0, 1]`` per input candidate, positionally aligned; the caller keeps
    its own order. An empty pool yields ``[]``. A caller with no taste centroid simply shouldn't
    call this (it would have no ``similarity`` to pass) and should leave ``confidence = None`` — the
    UI then shows the neutral "worth a look", and the cold-start path never regresses.
    """
    if not candidates:
        return []
    sim_rels = _relative_similarities([c.similarity for c in candidates])
    out: list[float | None] = []
    for candidate, sim_rel in zip(candidates, sim_rels, strict=True):
        _, components = score_candidate(candidate, taste, sim_rel=sim_rel)
        out.append(
            _confidence(
                taste,
                is_swing=False,
                sim_rel=components["similarity_rel"],
                sim_norm=components["similarity"],
                affinity_norm=components["affinity"],
                unproven=candidate.vote_count is not None
                and candidate.vote_count < _MIN_PROVEN_VOTES,
            )
        )
    return out


def rerank(
    candidates: Sequence[Candidate],
    taste: Mapping[str, Any],
    *,
    k: int = 12,
    swing_slots: int = 2,
    vote_mix: bool = False,
    facet_weights: Sequence[float] | None = None,
) -> list[Recommendation]:
    """Order candidates into a slate of up to ``k``, reserving ``swing_slots`` novelty picks.

    ``vote_mix=True`` (the chat path) ignores swing slots and instead composes a deliberate mix by
    vote count — ~50/35/15 well-known / lesser-known / low-vote — ordered by score, so the chat
    slate leads with the most *relevant* pick while still spanning a range of known-ness.

    ``facet_weights`` (round 10) is the taste-facet mass distribution behind a facet-merged pool;
    the main MMR selection then reserves slots per facet proportional to weight (floor of one for
    any facet ≥ ``_FACET_QUOTA_MIN_WEIGHT``) so one dominant mode can't sweep the whole slate — see
    :func:`_facet_quotas`. Ignored on the vote-mix path (chat composes by known-ness, and the
    per-facet similarity normalisation upstream already makes its score ordering facet-fair).
    """
    if not candidates:
        return []

    sim_rels = _relative_similarities([c.similarity for c in candidates])
    scored: list[tuple[float, Candidate, dict[str, float]]] = []
    for candidate, sim_rel in zip(candidates, sim_rels, strict=True):
        score, components = score_candidate(candidate, taste, sim_rel=sim_rel)
        scored.append((score, candidate, components))
    scored.sort(key=lambda item: item[0], reverse=True)
    # Collapse franchise instalments to one (best-scored) representative before composing any slate,
    # so no path shows "Rush Hour 2" *and* "Rush Hour 3". Runs on the score-sorted pool, so it keeps
    # the strongest sibling; both the vote-mix and MMR paths (and swings) inherit the deduped pool.
    scored = _dedupe_franchises(scored)

    if vote_mix:
        chosen = _select_vote_mix(scored, k)
        # A slate trimmed below k means the relevance floor dropped weak-fit tail candidates rather
        # than padding to k (principle 4). Surface it so a shorter chat slate is a visible, honest
        # call, never a silent quality erosion (G1). k is capped at the pool: asking for more than
        # exists isn't a relevance trim.
        if len(chosen) < min(k, len(scored)):
            record_fallback("reranker", "chat_slate_trimmed", requested=k, returned=len(chosen))
        return [
            _to_rec(candidate, score, components, taste, is_swing=False)
            for score, candidate, components in chosen
        ]

    swing_slots = max(0, min(swing_slots, k))
    main_slots = max(0, k - swing_slots)

    main = _select_diverse(
        scored, main_slots, quotas=_facet_quotas(facet_weights, scored, main_slots)
    )
    chosen_ids = {c.title_id for _, c, _ in main}

    # Swings: the most *novel* leftovers (lowest similarity), not the next-best by score. Novelty
    # reads the RAW cosine — a facet-merged candidate's ``similarity`` is facet-relative, and
    # "least like the taste" is a statement about the true embedding distance.
    leftovers = [item for item in scored if item[1].title_id not in chosen_ids]
    leftovers.sort(key=lambda item: _raw_similarity(item[1]))  # ascending = most novel first
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
        runtime_minutes=candidate.runtime_minutes,
        score=round(score, 4),
        is_swing=is_swing,
        confidence=_confidence(
            taste,
            is_swing=is_swing,
            sim_rel=components["similarity_rel"],
            sim_norm=components["similarity"],
            affinity_norm=components["affinity"],
            unproven=candidate.vote_count is not None and candidate.vote_count < _MIN_PROVEN_VOTES,
        ),
        poster_path=candidate.poster_path,
        overview=candidate.overview,
        keywords=list(candidate.keywords),
        components=components,
    )
