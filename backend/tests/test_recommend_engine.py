"""DB-backed engine tests: candidate generation, rows, and per-profile isolation.

Uses the local hash embedding provider so the real pgvector path runs with no credentials.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from phare.catalog.sample import seed_sample_catalog
from phare.db.models import Profile
from phare.ingest.sample import seed_sample_data
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider
from phare.providers.http import TTLCache
from phare.recommend.candidates import generate_candidates
from phare.recommend.rows import loved_seed_titles
from phare.recommend.service import RecommendationService
from phare.recommend.taste_vector import compute_taste_centroid, watched_title_ids


def _service(session: Session) -> RecommendationService:
    return RecommendationService(
        session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=None,
    )


def _seeded_profile(session: Session) -> uuid.UUID:
    profile = Profile(display_name="me")
    session.add(profile)
    session.flush()
    seed_sample_data(session, profile.id)
    seed_sample_catalog(session)
    session.flush()
    return profile.id


def test_centroid_is_memoized_per_request(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rows()/dynamic_rows fan out many recommend() calls; the centroid (which re-reads every watch
    # event) must be computed once per service instance, not per call.
    profile_id = _seeded_profile(db_session)
    service = _service(db_session)
    service.ensure_embeddings()

    calls = {"n": 0}
    real = compute_taste_centroid

    def counting(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr("phare.recommend.service.compute_taste_centroid", counting)

    service.recommend(profile_id)
    service.recommend(profile_id)
    assert calls["n"] == 1  # second call hit the cache


def test_candidates_exclude_watched(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    _service(db_session).ensure_embeddings()
    centroid = compute_taste_centroid(db_session, profile_id, LOCAL_MODEL_VERSION)
    assert centroid is not None

    watched = watched_title_ids(db_session, profile_id)
    candidates = generate_candidates(
        db_session, profile_id, centroid, LOCAL_MODEL_VERSION, limit=20
    )
    assert candidates  # the catalog gives us something to recommend
    assert all(c.title_id not in watched for c in candidates)


def test_candidates_respect_hard_avoids(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    _service(db_session).ensure_embeddings()
    centroid = compute_taste_centroid(db_session, profile_id, LOCAL_MODEL_VERSION)
    assert centroid is not None

    candidates = generate_candidates(
        db_session,
        profile_id,
        centroid,
        LOCAL_MODEL_VERSION,
        limit=50,
        hard_avoids=["Horror"],
    )
    assert candidates
    assert all("Horror" not in c.genres for c in candidates)


def test_rows_include_you_might_like_with_explanations(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    rows = _service(db_session).rows(profile_id)

    by_key = {row.key: row for row in rows}
    assert "you_might_like" in by_key
    yml = by_key["you_might_like"]
    assert yml.items
    assert all(item.explanation for item in yml.items)  # template fallback fills every one
    assert all(item.title_id not in watched_title_ids(db_session, profile_id) for item in yml.items)


def test_taste_driven_rows_do_not_repeat_titles(db_session: Session) -> None:
    # The "because you watched X" rows + you_might_like must not all lead with the same picks — with
    # a small catalog that made every row look identical (the review's "wall of the same dozen").
    profile_id = _seeded_profile(db_session)
    rows = {row.key: row for row in _service(db_session).rows(profile_id)}
    discovery = [r for k, r in rows.items() if k.startswith("because:") or k == "you_might_like"]
    assert len(discovery) >= 2  # there are several taste-driven rows to dedup across

    seen: set[str] = set()
    for row in discovery:
        ids = [str(item.title_id) for item in row.items]
        assert not (set(ids) & seen)  # no title appears in more than one taste-driven row
        seen.update(ids)


def test_rows_have_watch_again_and_continue_watching(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    by_key = {row.key: row for row in _service(db_session).rows(profile_id)}

    # Sample history rates Dune 9 and Severance 10 -> watch_again; Severance has episode history.
    assert "watch_again" in by_key
    assert any(item.title == "Dune" for item in by_key["watch_again"].items)
    assert "continue_watching" in by_key
    cont = by_key["continue_watching"]
    assert any(item.title == "Severance" for item in cont.items)
    # Each item now carries a real recency-decayed confidence, not a null placeholder.
    assert all(item.confidence is not None for item in cont.items)


def test_empty_profile_degrades_gracefully(db_session: Session) -> None:
    profile = Profile(display_name="newbie")
    db_session.add(profile)
    db_session.flush()
    seed_sample_catalog(db_session)

    rows = _service(db_session).rows(profile.id)
    # No history -> no centroid -> no you_might_like, no watch_again; but never an error.
    assert all(row.key != "you_might_like" for row in rows)


def test_recommendations_are_per_profile(db_session: Session) -> None:
    a = _seeded_profile(db_session)
    b_profile = Profile(display_name="other")
    db_session.add(b_profile)
    db_session.flush()
    # B has no history; A's watched titles must not leak into B's exclusion logic or vice versa.
    rows_a = _service(db_session).rows(a)
    assert rows_a  # A has recs
    assert watched_title_ids(db_session, b_profile.id) == set()  # isolation: B sees nothing of A


def test_loved_seed_titles_picks_loved(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    seeds = loved_seed_titles(db_session, profile_id, limit=3)
    assert seeds  # the sample history has highly-rated / loved titles
    assert len(seeds) <= 3
    assert len({s.id for s in seeds}) == len(seeds)  # distinct


def test_because_you_watched_rows_anchor_on_loved_titles(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    service = _service(db_session)
    service.ensure_embeddings()

    rows = service.because_you_watched_rows(profile_id, max_rows=3)

    assert rows
    assert all(row.key.startswith("because:") for row in rows)
    assert all(row.title.startswith("Because you watched ") for row in rows)
    watched = watched_title_ids(db_session, profile_id)
    for row in rows:
        assert row.items
        # Picks never include watched titles (so the seed itself can't recur).
        assert all(item.title_id not in watched for item in row.items)


def test_because_rows_render_before_you_might_like(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    keys = [row.key for row in _service(db_session).rows(profile_id)]

    because_idx = next((i for i, k in enumerate(keys) if k.startswith("because:")), None)
    assert because_idx is not None
    if "you_might_like" in keys:
        assert because_idx < keys.index("you_might_like")  # most-personalized first


# --- home-rows explanation budget + cache ------------------------------------
#
# A first real run showed the home page makes one LLM explanation call *per item* across rows,
# sequentially — dozens of slow calls against a real provider, blowing past the request timeout.
# FakeLLMProvider is instant, so latency is invisible here; instead we assert the invariant that
# actually bounds it — the *number* of LLM calls per render — which is deterministic and never
# flaky. The delay test then shows that bound translates into bounded wall-time.


def _llm_service(
    session: Session, llm: FakeLLMProvider, *, budget: int, swing_slots: int = 0
) -> RecommendationService:
    return RecommendationService(
        session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=llm,
        swing_slots=swing_slots,
        explanation_budget=budget,
    )


def test_home_rows_make_no_eager_llm_calls_by_default(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default budget is 0: rows render instantly on templates, and the LLM "why this" is left
    # to the lazy per-title endpoint. So a home render costs zero LLM explanation calls.
    monkeypatch.setattr("phare.recommend.service._EXPLANATION_CACHE", TTLCache(ttl=300))
    profile_id = _seeded_profile(db_session)
    llm = FakeLLMProvider(completion="A great fit for your taste.")
    service = RecommendationService(
        db_session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=llm,  # available, but default explanation_budget=0 means it's never called
    )

    rows = service.rows(profile_id)

    assert rows and all(item.explanation for r in rows for item in r.items)  # templates everywhere
    assert llm.prompts == []  # nothing explained eagerly


def test_home_rows_explanation_calls_are_bounded(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("phare.recommend.service._EXPLANATION_CACHE", TTLCache(ttl=300))
    profile_id = _seeded_profile(db_session)
    llm = FakeLLMProvider(completion="A great fit for your taste.")
    service = _llm_service(db_session, llm, budget=3)

    rows = service.rows(profile_id)

    total_items = sum(len(r.items) for r in rows)
    assert total_items > 3  # the page shows many items across rows...
    assert len(llm.prompts) <= 3  # ...but the LLM is called at most `budget` times, not per item
    assert all(item.explanation for r in rows for item in r.items)  # rest fall back to templates


def test_home_rows_reuse_cached_explanations(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("phare.recommend.service._EXPLANATION_CACHE", TTLCache(ttl=300))
    profile_id = _seeded_profile(db_session)
    llm = FakeLLMProvider(completion="A great fit for your taste.")
    service = _llm_service(db_session, llm, budget=1000)  # unbounded enough to cover the first page

    service.rows(profile_id)
    after_first = len(llm.prompts)
    assert after_first > 0  # the first render actually generated blurbs

    service.rows(profile_id)
    assert len(llm.prompts) == after_first  # the second render is fully served from the cache


def test_home_rows_latency_is_bounded_by_budget(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With a per-call delay, an unbounded per-item fan-out would scale with item count; the budget
    # caps both calls and wall-time. Tiny delay + generous bound keep this fast and non-flaky.
    import time

    monkeypatch.setattr("phare.recommend.service._EXPLANATION_CACHE", TTLCache(ttl=300))
    profile_id = _seeded_profile(db_session)
    llm = FakeLLMProvider(completion="A great fit for your taste.", complete_delay=0.01)
    service = _llm_service(db_session, llm, budget=3)

    start = time.perf_counter()
    service.rows(profile_id)
    elapsed = time.perf_counter() - start

    assert len(llm.prompts) <= 3
    assert elapsed < 0.5  # ~3 * 10ms of LLM time + DB work — not item-count * 10ms
