"""Evaluation: pure metrics, the persona guardrail suite, and degeneracy detection."""

from __future__ import annotations

import uuid

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
        db_session, embed_provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
    )
    assert len(results) == len(PERSONAS)
    for result in results:
        assert result.count > 0, f"{result.name} got no recommendations"
        assert result.passed, (
            f"{result.name} violated guardrails: "
            f"{result.forbidden_violations} / {result.recommended_watched}"
        )


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
    assert all("Science Fiction" in r.genres for r in recs)


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
    assert all("Science Fiction" in item.genres for item in reply.items)
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
