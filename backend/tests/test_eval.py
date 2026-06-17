"""Evaluation: pure metrics, the persona guardrail suite, and degeneracy detection."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from phare.eval import metrics
from phare.eval.harness import evaluate_all, evaluate_persona
from phare.eval.personas import PERSONAS, Persona
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider

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
