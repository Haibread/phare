"""The re-ranker is the deterministic heart of the engine — test it hard, with synthetic data.

No DB, no LLM, no clock: every assertion is a pure consequence of the inputs.
"""

from __future__ import annotations

import uuid
from typing import Any

from phare.recommend.reranker import rerank, score_candidate
from phare.recommend.schema import Candidate


def _cand(
    *,
    title: str,
    sim: float,
    genres: list[str] | None = None,
    keywords: list[str] | None = None,
    popularity: float | None = None,
    vote_count: int | None = None,
    vote_average: float | None = None,
) -> Candidate:
    return Candidate(
        title_id=uuid.uuid4(),
        title=title,
        kind="movie",
        year=2020,
        genres=genres or [],
        keywords=keywords or [],
        runtime_minutes=120,
        popularity=popularity,
        vote_count=vote_count,
        vote_average=vote_average,
        overview=None,
        similarity=sim,
    )


def test_deterministic() -> None:
    cands = [_cand(title=f"t{i}", sim=0.5 - i * 0.05, genres=["Drama"]) for i in range(6)]
    first = rerank(cands, {}, k=4, swing_slots=1)
    second = rerank(cands, {}, k=4, swing_slots=1)
    assert [r.title for r in first] == [r.title for r in second]


def test_affinity_reorders_equal_similarity() -> None:
    taste: dict[str, Any] = {"affinities": {"Horror": 0.9}}
    horror = _cand(title="Scary", sim=0.4, genres=["Horror"])
    drama = _cand(title="Sad", sim=0.4, genres=["Drama"])
    recs = rerank([drama, horror], taste, k=2, swing_slots=0)
    assert recs[0].title == "Scary"  # positive affinity wins the tie


def test_affinity_operant_via_free_key() -> None:
    # H1: a free affinity key ("Sci-Fi") used to miss the catalog's "Science Fiction" tag on exact
    # match, leaving affinity flat at neutral (0.5) for everyone. Now it lines up via the matcher.
    taste: dict[str, Any] = {"affinities": {"Sci-Fi": 0.9}}
    scifi = _cand(title="Arrival", sim=0.4, genres=["Science Fiction"])
    comedy = _cand(title="Superbad", sim=0.4, genres=["Comedy"])
    recs = {r.title: r for r in rerank([scifi, comedy], taste, k=2, swing_slots=0)}
    assert recs["Arrival"].components["affinity"] > recs["Superbad"].components["affinity"]
    assert recs["Superbad"].components["affinity"] == 0.5  # no match → neutral, not a false hit


def test_affinity_distribution_has_nonzero_variance() -> None:
    # Anti-regression for H1 (affinity was a constant 0.5 for the whole pool). With a marked profile
    # over a varied pool, the affinity component must actually vary title-to-title.
    taste: dict[str, Any] = {"affinities": {"Science Fiction": 0.9, "Horror": -0.6}}
    pool = [
        _cand(title="a", sim=0.4, genres=["Science Fiction"]),
        _cand(title="b", sim=0.4, genres=["Horror"]),
        _cand(title="c", sim=0.4, genres=["Comedy"]),
    ]
    recs = rerank(pool, taste, k=3, swing_slots=0)
    assert len({r.components["affinity"] for r in recs}) >= 2


def test_popularity_penalty_demotes_blockbuster() -> None:
    niche = _cand(title="Indie", sim=0.4, genres=["Drama"], popularity=1.0)
    blockbuster = _cand(title="Blockbuster", sim=0.4, genres=["Action"], popularity=100.0)
    recs = rerank([blockbuster, niche], {}, k=2, swing_slots=0)
    assert recs[0].title == "Indie"  # popularity is a penalty, never a boost


def test_diversity_breaks_single_genre_dominance() -> None:
    # Five dramas + one comedy of competitive (not pool-minimum) similarity. A pure score sort would
    # bury the comedy under the drama pile; MMR must surface it into a 3-slot slate. (Sims sit in a
    # tight band so the comedy is a real contender once placed relative to the pool — H2.)
    dramas = [_cand(title=f"Drama{i}", sim=0.6 - i * 0.01, genres=["Drama"]) for i in range(5)]
    comedy = _cand(title="Comedy", sim=0.59, genres=["Comedy"])
    recs = rerank([*dramas, comedy], {}, k=3, swing_slots=0)
    assert "Comedy" in [r.title for r in recs]


def test_swing_slots_are_reserved_and_flagged() -> None:
    cands = [_cand(title=f"t{i}", sim=0.9 - i * 0.1, genres=["Drama"]) for i in range(8)]
    recs = rerank(cands, {}, k=5, swing_slots=2)
    swings = [r for r in recs if r.is_swing]
    assert len(swings) == 2
    # Swings are the novel tail: lower similarity than the main picks, and hedged on confidence.
    main_min_sim = min(
        c.similarity for c in cands for r in recs if not r.is_swing and c.title == r.title
    )
    swing_sims = [c.similarity for c in cands for r in swings if c.title == r.title]
    assert all(s <= main_min_sim for s in swing_sims)
    assert all((r.confidence or 0) < 0.6 for r in swings)


def test_confidence_reflects_affinity_spread() -> None:
    # Two equally-similar titles; one hits a liked genre, one doesn't. Confidence must separate them
    # so a row isn't a flat wall of one label (review: everything reading "Strong fit").
    liked = _cand(title="Liked", sim=0.7, genres=["Science Fiction"])
    offaxis = _cand(title="Offaxis", sim=0.7, genres=["Western"])
    taste = {"affinities": {"Science Fiction": 1.0, "Western": -0.5}, "confidence": 0.5}
    recs = {r.title: r for r in rerank([liked, offaxis], taste, k=2, swing_slots=0)}
    assert (recs["Liked"].confidence or 0) > (recs["Offaxis"].confidence or 0)


def test_confidence_tracks_relative_similarity_without_taste() -> None:
    # With no taste profile, affinity carries no signal — confidence is the *pool-relative* sim
    # read (H2/A8), so picks separate into different buckets instead of all reading one label.
    cands = [_cand(title=f"t{i}", sim=s, genres=["Drama"]) for i, s in enumerate([0.9, 0.5, 0.1])]
    recs = {r.title: r for r in rerank(cands, {}, k=3, swing_slots=0)}
    assert (
        (recs["t0"].confidence or 0) > (recs["t1"].confidence or 0) > (recs["t2"].confidence or 0)
    )


def test_similarity_is_normalized_relative_to_pool() -> None:
    # H2: compressed cosines (0.75..0.84) barely move on the absolute scale, so scoring off them
    # makes everything look equally good. Placed relative to the pool, they spread across [0,1]. The
    # raw absolute value stays in components["similarity"]; ["similarity_rel"] is what scores.
    sims = [0.75, 0.78, 0.80, 0.82, 0.84]
    cands = [_cand(title=f"t{i}", sim=s) for i, s in enumerate(sims)]
    recs = rerank(cands, {}, k=5, swing_slots=0)
    rels = sorted(r.components["similarity_rel"] for r in recs)
    assert rels[0] < 0.4 and rels[-1] > 0.6  # genuinely spread, not a flat 0.87..0.92
    assert all({"similarity", "similarity_rel"} <= set(r.components) for r in recs)


def test_relative_similarity_is_neutral_for_tiny_or_flat_pool() -> None:
    # Nothing to normalise against → neutral 0.5 for all (never invent a spread).
    two = rerank([_cand(title="a", sim=0.9), _cand(title="b", sim=0.1)], {}, k=2, swing_slots=0)
    assert all(r.components["similarity_rel"] == 0.5 for r in two)  # pool < 3
    flat = rerank([_cand(title=f"t{i}", sim=0.8) for i in range(4)], {}, k=4, swing_slots=0)
    assert all(r.components["similarity_rel"] == 0.5 for r in flat)  # zero spread


def test_confidence_capped_by_thin_history() -> None:
    # A8: a barely-evidenced profile (taste confidence 0.2) can't emit "strong fit" for anything —
    # output confidence is capped at 0.35 + 0.65*0.2 = 0.48, even for a top-of-pool, on-genre pick.
    taste = {"affinities": {"Drama": 1.0}, "confidence": 0.2}
    cands = [_cand(title=f"t{i}", sim=s, genres=["Drama"]) for i, s in enumerate([0.95, 0.5, 0.2])]
    recs = rerank(cands, taste, k=3, swing_slots=0)
    assert all((r.confidence or 0) <= 0.48 for r in recs)


def test_confidence_distribution_spans_multiple_buckets() -> None:
    # A8/H2: confidence used to read one label for the whole slate. With pool-relative similarity it
    # spreads. Bucket by the frontend thresholds (fit.ts: >=0.66 strong, >=0.4 mid, else low).
    cands = [
        _cand(title=f"t{i}", sim=s, genres=["Drama"]) for i, s in enumerate([0.9, 0.6, 0.3, 0.1])
    ]
    recs = rerank(cands, {}, k=4, swing_slots=0)
    buckets = {
        2 if (r.confidence or 0) >= 0.66 else 1 if (r.confidence or 0) >= 0.4 else 0 for r in recs
    }
    assert len(buckets) >= 2


def test_swing_slots_clamped_to_available() -> None:
    recs = rerank([_cand(title="only", sim=0.5)], {}, k=5, swing_slots=2)
    assert len(recs) == 1  # never invents items it doesn't have


def test_empty_candidates() -> None:
    assert rerank([], {}, k=5, swing_slots=2) == []


def test_score_components_are_transparent() -> None:
    score, components = score_candidate(_cand(title="t", sim=0.0, popularity=40.0), {})
    assert set(components) == {
        "similarity",
        "similarity_rel",
        "affinity",
        "popularity_penalty",
        "quality_penalty",
        "score",
    }
    assert components["similarity"] == 0.5  # sim 0.0 -> normalised 0.5
    assert components["popularity_penalty"] == 0.5  # 40 / cap(80)


# --- vote-mix (the chat slate): mix by vote count, ordered most-voted-first ------------------


def test_vote_mix_orders_the_slate_by_score_not_votes() -> None:
    # The vote mix picks *which* titles are on the slate (a spread of known-ness); ordering is by
    # score, so the best match leads even when it's the least-voted (review A1: votes were burying
    # the strongest pick at the bottom of the strip).
    cands = [
        _cand(title="obscure", sim=0.9, vote_count=50),
        _cand(title="megahit", sim=0.1, vote_count=30_000),
        _cand(title="midsize", sim=0.5, vote_count=900),
    ]
    out = rerank(cands, {}, k=3, vote_mix=True)
    # Score = (sim+1)/2 with no penalties → obscure 0.95 > midsize 0.75 > megahit 0.55.
    assert [r.title for r in out] == ["obscure", "midsize", "megahit"]


def test_quality_penalty_demotes_poorly_rated_title() -> None:
    # Two equally-similar, equally-obscure titles; the badly-rated one is held back below the
    # well-rated one. A floor, not a boost — vote_average never *lifts* a title above its peers.
    good = _cand(title="Acclaimed", sim=0.4, genres=["Drama"], vote_average=7.5)
    bad = _cand(title="Panned", sim=0.4, genres=["Drama"], vote_average=4.0)
    recs = rerank([bad, good], {}, k=2, swing_slots=0)
    assert recs[0].title == "Acclaimed"


def test_quality_penalty_is_zero_without_a_rating() -> None:
    # No vote_average → no penalty at all (never guess a title is bad because TMDB is silent).
    _, components = score_candidate(_cand(title="t", sim=0.4), {})
    assert components["quality_penalty"] == 0.0


def test_vote_mix_composes_a_mix_not_just_the_most_popular() -> None:
    # 10 well-known, 10 lesser-known, 10 low-vote candidates; a k=10 slate should pull from all
    # three tiers (~5 / ~3-4 / ~1-2), not just the top-voted ten.
    cands = (
        [_cand(title=f"hit{i}", sim=0.5, vote_count=5_000 + i) for i in range(10)]
        + [_cand(title=f"mid{i}", sim=0.5, vote_count=800 + i) for i in range(10)]
        + [_cand(title=f"low{i}", sim=0.5, vote_count=50 + i) for i in range(10)]
    )
    out = rerank(cands, {}, k=10, vote_mix=True)
    titles = [r.title for r in out]
    assert sum(t.startswith("hit") for t in titles) == 5  # ~50%
    assert sum(t.startswith("mid") for t in titles) == 4  # ~35% (largest-remainder rounding)
    assert sum(t.startswith("low") for t in titles) == 1  # ~15%


def test_vote_mix_backfills_when_a_tier_is_empty() -> None:
    # No well-known titles at all (thin catalog): the slate still fills to k from what's available.
    cands = [_cand(title=f"low{i}", sim=0.5, vote_count=20 + i) for i in range(6)]
    out = rerank(cands, {}, k=5, vote_mix=True)
    assert len(out) == 5


def test_unproven_low_vote_title_confidence_is_capped() -> None:
    # A9: a just-released title with almost no votes can't read as a confident pick, even top-of-
    # pool and on-genre — recommending an unwatched-by-anyone title with confidence is dishonest.
    taste: dict[str, Any] = {"affinities": {"Drama": 1.0}, "confidence": 0.9}
    fresh = _cand(title="Fresh 2026", sim=0.9, genres=["Drama"], vote_count=30)
    proven = _cand(title="Classic", sim=0.9, genres=["Drama"], vote_count=8000)
    recs = {r.title: r for r in rerank([fresh, proven], taste, k=2, swing_slots=0)}
    assert (recs["Fresh 2026"].confidence or 0) <= 0.5
    assert (recs["Classic"].confidence or 0) > (recs["Fresh 2026"].confidence or 0)


def test_unknown_vote_count_is_not_treated_as_unproven() -> None:
    # None votes = unknown, not low — don't penalise a title we simply haven't refreshed yet.
    taste: dict[str, Any] = {"affinities": {"Drama": 1.0}, "confidence": 0.9}
    known = _cand(title="Known", sim=0.9, genres=["Drama"], vote_count=8000)
    unknown = _cand(title="Unknown", sim=0.9, genres=["Drama"], vote_count=None)
    recs = {r.title: r for r in rerank([known, unknown], taste, k=2, swing_slots=0)}
    assert recs["Unknown"].confidence == recs["Known"].confidence  # not force-capped
