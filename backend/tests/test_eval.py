"""Evaluation: pure metrics, the persona guardrail suite, and degeneracy detection."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.agent.schema import ChatIntent
from phare.agent.service import ChatService, intent_filter
from phare.catalog.sample import seed_sample_catalog
from phare.db.models import EventType, Profile, Title, WatchEvent
from phare.eval import metrics
from phare.eval.harness import evaluate_all, evaluate_persona
from phare.eval.personas import PERSONAS, Persona
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider
from phare.recommend import genres as genre_match
from phare.recommend.schema import Recommendation
from phare.recommend.service import RecommendationService

# --- pure metrics -----------------------------------------------------------


def test_popularity_bias_and_novelty_are_complementary() -> None:
    pops = [80.0, 40.0, 0.0]  # normalised: 1.0, 0.5, 0.0 -> mean 0.5
    assert metrics.popularity_bias(pops) == 0.5
    assert metrics.novelty(pops) == 0.5


def test_intra_list_diversity_extremes() -> None:
    same = [["Drama"], ["Drama"], ["Drama"]]
    assert metrics.intra_list_diversity(same) == 0.0  # degenerate: all identical
    distinct = [["Drama"], ["Comedy"], ["Horror"]]
    assert metrics.intra_list_diversity(distinct) == 1.0


def test_recall_at_k_and_coverage() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert metrics.recall_at_k([a, b], [b, c]) == 0.5
    assert metrics.catalog_coverage([a, a, b], catalog_size=4) == 0.5


def test_degenerate_slate_is_flagged_by_metrics() -> None:
    # "Shawshank to everyone forever": all blockbusters, all one genre.
    degenerate_pops = [100.0] * 10
    degenerate_genres = [["Drama"]] * 10
    assert metrics.popularity_bias(degenerate_pops) == 1.0  # maxed out
    assert metrics.intra_list_diversity(degenerate_genres) == 0.0  # no diversity
    # A healthy slate clears sane thresholds the degenerate one fails.
    healthy_pops = [10.0, 30.0, 5.0, 50.0]
    healthy_genres = [["Drama"], ["Comedy"], ["Horror", "Thriller"], ["Science Fiction"]]
    assert metrics.popularity_bias(healthy_pops) < 0.6
    assert metrics.intra_list_diversity(healthy_genres) > 0.5


# --- persona guardrail suite (DB-backed, offline embedder) ------------------


def test_all_personas_pass_guardrails(db_session: Session) -> None:
    results = evaluate_all(
        db_session,
        embed_provider=LocalHashEmbeddingProvider(),
        model_version=LOCAL_MODEL_VERSION,
    )
    assert len(results) == len(PERSONAS)
    for result in results:
        assert result.count > 0, f"{result.name} got no recommendations"
        assert result.passed, (
            f"{result.name} violated guardrails: "
            f"{result.forbidden_violations} / {result.recommended_watched}"
        )


def test_all_personas_pass_alignment_checks(db_session: Session) -> None:
    # M10.2: the harness now also asserts taste alignment (hard-avoids, affinity variance, score
    # order, similarity spread). With the offline embedder every persona must still clear them.
    results = evaluate_all(
        db_session,
        embed_provider=LocalHashEmbeddingProvider(),
        model_version=LOCAL_MODEL_VERSION,
    )
    for result in results:
        assert result.alignment_failures == [], (
            f"{result.name} failed alignment: {result.alignment_failures}"
        )


def test_alignment_summary_flags_offline_spread_skip() -> None:
    # The header must be honest that similarity-spread does not run on the offline embedder.
    from phare.eval.harness import alignment_checks_summary

    offline = alignment_checks_summary(LOCAL_MODEL_VERSION)
    assert "score-order" in offline and "affinity-variance" in offline
    assert "skipped" in offline  # spread is not exercised on local-hash
    assert "skipped" not in alignment_checks_summary("real-prod-embed-v1")


def test_empty_slate_is_skipped_offline_but_fails_on_real_embedder() -> None:
    # An empty slate is an embedder artifact on the offline local-hash space (several personas have
    # no neighbours there), so it must NOT fail the guardrail offline — but on the real production
    # embedding space an empty slate is a genuine alignment failure (harness._alignment_failures).
    from phare.eval.harness import _alignment_failures

    persona = next(p for p in PERSONAS if p.name == "comedy-only")
    assert _alignment_failures([], persona, model_version=LOCAL_MODEL_VERSION) == []
    real = _alignment_failures([], persona, model_version="real-prod-embed-v1")
    assert real and any("empty slate" in reason for reason in real)


def test_alignment_summary_flags_offline_empty_slate_skip() -> None:
    # The header must be honest that empty slates are not failed on the offline embedder.
    from phare.eval.harness import alignment_checks_summary

    assert "empty-slate" in alignment_checks_summary(LOCAL_MODEL_VERSION)
    assert "empty-slate" not in alignment_checks_summary("real-prod-embed-v1")


def _aligned_rec(title: str, affinity: float) -> Recommendation:
    return Recommendation(
        title_id=uuid.uuid4(),
        title=title,
        kind="movie",
        year=2020,
        genres=["Comedy"],
        score=1.0,
        components={"affinity": affinity, "similarity_rel": 0.6, "score": 1.0},
    )


def test_alignment_passes_a_slate_flat_at_high_affinity() -> None:
    # A slate flat at a *non-neutral* affinity is the taste key matching everything (a perfectly
    # on-genre slate, e.g. comedy-only over a comedy-heavy pool) — the goal, not the H1 bug. The
    # relevance floor can trim a slate down to exactly this; it must not read as a flat-affinity
    # failure. (Regression: the check used to fail any single-distinct-value column.)
    from phare.eval.harness import _alignment_failures

    persona = next(p for p in PERSONAS if p.name == "comedy-only")
    slate = [_aligned_rec(f"c{i}", affinity=0.95) for i in range(6)]
    failures = _alignment_failures(slate, persona, model_version="real-prod-embed-v1")
    assert not any("affinity is flat" in reason for reason in failures)


def test_alignment_fails_a_slate_flat_at_neutral_affinity() -> None:
    # The H1 bug signature: the taste key matched nothing, so every candidate scored the neutral
    # 0.5. That must still fail — this is exactly what the check exists to catch.
    from phare.eval.harness import _alignment_failures

    persona = next(p for p in PERSONAS if p.name == "comedy-only")
    slate = [_aligned_rec(f"c{i}", affinity=0.5) for i in range(6)]
    failures = _alignment_failures(slate, persona, model_version="real-prod-embed-v1")
    assert any("affinity is flat" in reason for reason in failures)


def _conf_rec(title: str, confidence: float, sim_rel: float) -> Recommendation:
    # A rec whose confidence and pool-relative similarity we control, for the anti-uniformity check.
    # affinity varies with sim_rel so the affinity-variance check never masks the uniformity result.
    return Recommendation(
        title_id=uuid.uuid4(),
        title=title,
        kind="movie",
        year=2020,
        genres=["Comedy"],
        score=1.0,
        confidence=confidence,
        components={"affinity": sim_rel, "similarity_rel": sim_rel, "score": 1.0},
    )


def test_alignment_fails_a_uniform_fit_slate() -> None:
    # R2: every displayed item in the same fit bucket is a badge carrying no information (the owner
    # complained every card reads 3/3 "strong fit"). On the real embedder that must fail. sim_rel is
    # spread so *only* the uniformity check bites, not the similarity-spread one.
    from phare.eval.harness import _alignment_failures

    persona = next(p for p in PERSONAS if p.name == "comedy-only")
    # All confidences >= _FIT_STRONG (0.72) → all bucket 2, but sim_rel genuinely spreads.
    slate = [_conf_rec(f"c{i}", confidence=0.80, sim_rel=0.3 + 0.1 * i) for i in range(6)]
    failures = _alignment_failures(slate, persona, model_version="real-prod-embed-v1")
    assert any("fit chips are uniform" in reason for reason in failures)


def test_alignment_passes_a_multi_bucket_fit_slate() -> None:
    # A slate whose confidences land in more than one fit bucket is exactly the goal — it must not
    # trip the uniformity check.
    from phare.eval.harness import _alignment_failures

    persona = next(p for p in PERSONAS if p.name == "comedy-only")
    confs = [0.90, 0.78, 0.55, 0.50, 0.40, 0.30]  # spans strong / worth-a-try / long-shot
    slate = [_conf_rec(f"c{i}", confidence=c, sim_rel=0.3 + 0.1 * i) for i, c in enumerate(confs)]
    failures = _alignment_failures(slate, persona, model_version="real-prod-embed-v1")
    assert not any("fit chips are uniform" in reason for reason in failures)


def test_uniform_fit_slate_is_skipped_offline_and_on_tiny_slates() -> None:
    # The uniformity check is gated exactly like the similarity-spread one: skipped on the offline
    # local-hash embedder (not the production space) and on slates too small to judge (< 6 items).
    from phare.eval.harness import _alignment_failures

    persona = next(p for p in PERSONAS if p.name == "comedy-only")
    uniform_big = [_conf_rec(f"c{i}", confidence=0.80, sim_rel=0.3 + 0.1 * i) for i in range(6)]
    # Offline: the whole spread family of checks is relaxed, so no uniformity failure.
    assert not any(
        "fit chips are uniform" in r
        for r in _alignment_failures(uniform_big, persona, model_version=LOCAL_MODEL_VERSION)
    )
    # Tiny slate on the real embedder: too few items to call it uniform (a 5-item cluster is fine).
    uniform_small = [_conf_rec(f"c{i}", confidence=0.80, sim_rel=0.3 + 0.1 * i) for i in range(5)]
    assert not any(
        "fit chips are uniform" in r
        for r in _alignment_failures(uniform_small, persona, model_version="real-prod-embed-v1")
    )


def test_alignment_summary_names_the_fit_uniformity_check() -> None:
    # The header must name the anti-uniformity check, and say it's skipped offline.
    from phare.eval.harness import alignment_checks_summary

    assert "fit-uniformity" in alignment_checks_summary("real-prod-embed-v1")
    assert "fit-uniformity" in alignment_checks_summary(LOCAL_MODEL_VERSION)
    assert "skipped" in alignment_checks_summary(LOCAL_MODEL_VERSION)


def test_alignment_fails_on_flat_affinity(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M10.2 validation: simulate a taste key that matches nothing (constant affinity) — the harness
    # must FAIL the persona with the flat-affinity reason, not quietly show green (review H1).
    monkeypatch.setattr(
        "phare.recommend.reranker._affinity_score", lambda candidate, affinities: 0.0
    )
    persona = next(p for p in PERSONAS if p.name == "comedy-only")
    result = evaluate_persona(
        db_session,
        persona,
        embed_provider=LocalHashEmbeddingProvider(),
        model_version=LOCAL_MODEL_VERSION,
    )
    assert not result.passed
    assert any("affinity is flat" in reason for reason in result.alignment_failures)


def test_gore_avoider_never_sees_forbidden_genre(db_session: Session) -> None:
    persona = next(p for p in PERSONAS if p.name == "gore-avoider")
    result = evaluate_persona(
        db_session,
        persona,
        embed_provider=LocalHashEmbeddingProvider(),
        model_version=LOCAL_MODEL_VERSION,
    )
    assert result.forbidden_violations == []


# --- alignment: lock what the user actually feels (review H7 / K1, mission M1.7) --------------
#
# The pre-M1 suite only checked structure (a row exists, a genre never leaks) — A1, H1 and A8 all
# passed a green suite. These assert the *relevance mechanics* a regression would silently break.


def _aligned_service(session: Session) -> tuple[RecommendationService, uuid.UUID]:
    """A seeded catalog + a profile with a couple of watched Dramas (so a centroid exists)."""
    seed_sample_catalog(session)
    profile = Profile(display_name="align")
    session.add(profile)
    session.flush()
    for tmdb_id in (238, 8967):  # Godfather, There Will Be Blood — both Drama, never Horror
        title = session.scalar(select(Title).where(Title.tmdb_id == tmdb_id))
        session.add(
            WatchEvent(
                profile_id=profile.id,
                title_id=title.id,
                type=EventType.watched,
                source="eval",
                external_ref=f"eval:{tmdb_id}",
            )
        )
    session.flush()
    service = RecommendationService(
        session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=None,
    )
    service.ensure_embeddings()
    return service, profile.id


def test_chat_slate_is_ordered_by_score(db_session: Session) -> None:
    # A1: the chat slate must read score-descending. Reintroducing the vote-count sort breaks
    # monotonicity even though every structural test would still pass.
    service, profile_id = _aligned_service(db_session)
    recs = service.recommend(
        profile_id, taste={"affinities": {"Science Fiction": 0.9}, "confidence": 0.6}, vote_mix=True
    )
    scores = [r.score for r in recs]
    assert recs and scores == sorted(scores, reverse=True)


def test_genre_filter_uses_a_free_key_not_exact_match(db_session: Session) -> None:
    # A2 (discriminating): a *free* genre "sci-fi" must resolve to the catalog's "Science Fiction"
    # tag. Under the old exact-lowercase intersection this matched nothing, the filter silently
    # returned the whole pool, and the slate was NOT all sci-fi — so this fails on the old code.
    service, profile_id = _aligned_service(db_session)
    recs = service.recommend(
        profile_id,
        candidate_filter=intent_filter(ChatIntent(include_genres=["sci-fi"])),
        vote_mix=True,
    )
    assert recs, "expected a non-empty slate"
    # The invariant is that every returned title matches the requested "sci-fi" key — which now
    # covers TV's "Sci-Fi & Fantasy" as well as film's "Science Fiction". Under the old exact-match
    # code the filter was a no-op and the slate mixed in non-sci-fi titles.
    assert all(genre_match.matches_any(["sci-fi"], r.genres) for r in recs)


def test_affinity_operant_via_free_key_in_the_engine(db_session: Session) -> None:
    # H1 (discriminating): a free affinity key ("Sci-Fi") must line up with the "Science Fiction"
    # tag. Under the old exact match every candidate scored a flat 0.5 (one distinct value) — so the
    # >= 2 assertion fails on the old code.
    service, profile_id = _aligned_service(db_session)
    recs = service.recommend(
        profile_id,
        taste={"affinities": {"Sci-Fi": 0.9, "true crime": -0.5}, "confidence": 0.6},
        vote_mix=True,
        k=12,
    )
    assert len({r.components.get("affinity") for r in recs}) >= 2


def test_chat_planner_free_genre_is_actually_applied(db_session: Session) -> None:
    # A2 through the chat path (the exact scenario from the review): the planner emits a free genre
    # "sci-fi"; the slate must be Science Fiction and score-ordered. Fails on the old no-op filter.
    service, profile_id = _aligned_service(db_session)
    planner_llm = FakeLLMProvider(
        completion='{"calls":[{"tool":"recommend","args":{"include_genres":["sci-fi"]}}]}'
    )
    reply = ChatService(service, chat_llm=planner_llm).respond(profile_id, "a slow-burn sci-fi")
    assert reply.items, "expected a non-empty chat slate"
    assert all(genre_match.matches_any(["sci-fi"], item.genres) for item in reply.items)
    scores = [item.score for item in reply.items]
    assert scores == sorted(scores, reverse=True)


def test_guardrail_catches_a_hard_avoid_regression(db_session: Session) -> None:
    # A broken taste profile that forbids Horror but (simulating a bug) doesn't avoid it:
    # we forbid "Drama" while the persona's centroid points straight at dramas it watched, and
    # we DON'T pass it as a hard_avoid — so the slate should contain Drama and the guard fires.
    broken = Persona(
        name="leaky",
        watched=[(238, 9.0), (8967, 9.0)],  # Godfather + There Will Be Blood (Drama)
        taste={"affinities": {"Drama": 0.9}},  # no hard_avoids
        forbidden_genres=("Drama",),
    )
    result = evaluate_persona(
        db_session,
        broken,
        embed_provider=LocalHashEmbeddingProvider(),
        model_version=LOCAL_MODEL_VERSION,
    )
    assert result.forbidden_violations, "the guardrail should detect the unfiltered Drama leak"
