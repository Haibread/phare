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


def test_popularity_penalty_demotes_blockbuster() -> None:
    niche = _cand(title="Indie", sim=0.4, genres=["Drama"], popularity=1.0)
    blockbuster = _cand(title="Blockbuster", sim=0.4, genres=["Action"], popularity=100.0)
    recs = rerank([blockbuster, niche], {}, k=2, swing_slots=0)
    assert recs[0].title == "Indie"  # popularity is a penalty, never a boost


def test_diversity_breaks_single_genre_dominance() -> None:
    # Five high-similarity dramas + one slightly-lower comedy. A pure score sort would bury the
    # comedy; MMR must surface it into a 3-slot slate.
    dramas = [_cand(title=f"Drama{i}", sim=0.6 - i * 0.01, genres=["Drama"]) for i in range(5)]
    comedy = _cand(title="Comedy", sim=0.5, genres=["Comedy"])
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


def test_confidence_is_similarity_only_without_taste() -> None:
    # With no taste profile, affinity carries no signal — confidence shouldn't be dragged toward
    # neutral; it stays the pure similarity read.
    cand = _cand(title="t", sim=0.6, genres=["Drama"])
    (rec,) = rerank([cand], {}, k=1, swing_slots=0)
    assert rec.confidence == round((0.6 + 1.0) / 2.0, 3)


def test_swing_slots_clamped_to_available() -> None:
    recs = rerank([_cand(title="only", sim=0.5)], {}, k=5, swing_slots=2)
    assert len(recs) == 1  # never invents items it doesn't have


def test_empty_candidates() -> None:
    assert rerank([], {}, k=5, swing_slots=2) == []


def test_score_components_are_transparent() -> None:
    score, components = score_candidate(_cand(title="t", sim=0.0, popularity=40.0), {})
    assert set(components) == {"similarity", "affinity", "popularity_penalty", "score"}
    assert components["similarity"] == 0.5  # sim 0.0 -> normalised 0.5
    assert components["popularity_penalty"] == 0.5  # 40 / cap(80)


# --- vote-mix (the chat slate): mix by vote count, ordered most-voted-first ------------------


def test_vote_mix_orders_the_slate_by_vote_count() -> None:
    cands = [
        _cand(title="obscure", sim=0.9, vote_count=50),
        _cand(title="megahit", sim=0.1, vote_count=30_000),
        _cand(title="midsize", sim=0.5, vote_count=900),
    ]
    out = rerank(cands, {}, k=3, vote_mix=True)
    # Despite "obscure" having the best similarity, the slate reads most-voted-first.
    assert [r.title for r in out] == ["megahit", "midsize", "obscure"]


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
