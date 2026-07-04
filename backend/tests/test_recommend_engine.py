"""DB-backed engine tests: candidate generation, rows, and per-profile isolation.

Uses the local hash embedding provider so the real pgvector path runs with no credentials.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from phare.catalog.sample import seed_sample_catalog
from phare.db.models import Profile, Title, TitleKind
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


def test_enrich_runtimes_fills_pool_from_provider_and_persists(db_session: Session) -> None:
    from phare.db.models import Title, TitleKind
    from phare.providers.fakes import FakeMetadataProvider
    from phare.providers.types import TitleMetadata
    from phare.recommend.schema import Candidate

    titles = []
    for i in range(3):
        title = Title(kind=TitleKind.movie, title=f"Film {i}", tmdb_id=2000 + i)
        db_session.add(title)
        titles.append(title)
    db_session.flush()
    provider = FakeMetadataProvider(
        titles={
            (t.tmdb_id, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie, title=t.title, runtime_minutes=90 + i
            )
            for i, t in enumerate(titles)
        }
    )

    def cand(title: Title, runtime: int | None) -> Candidate:
        return Candidate(
            title_id=title.id,
            title=title.title,
            kind="movie",
            year=None,
            genres=[],
            keywords=[],
            runtime_minutes=runtime,
            popularity=None,
            overview=None,
            similarity=0.5,
        )

    # Two candidates lack runtime; the third already has one and must be left untouched (no fetch).
    candidates = [cand(titles[0], None), cand(titles[1], None), cand(titles[2], 120)]
    enriched = _service(db_session)._enrich_runtimes(candidates, provider)

    assert [c.runtime_minutes for c in enriched] == [90, 91, 120]  # the two missing got filled
    assert titles[0].runtime_minutes == 90 and titles[1].runtime_minutes == 91  # persisted to DB
    assert titles[2].runtime_minutes is None  # never fetched — its candidate already had runtime
    assert {tmdb for tmdb, _ in provider.calls} == {2000, 2001}  # only the missing ones fetched


def test_enrich_runtimes_short_circuits_when_pool_is_already_filled(db_session: Session) -> None:
    # The common case once a taste's pool is enriched: nothing missing → return as-is, no provider
    # call and no (empty) DB query on the hot path.
    from phare.providers.fakes import FakeMetadataProvider
    from phare.recommend.schema import Candidate

    candidates = [
        Candidate(
            title_id=uuid.uuid4(),
            title=name,
            kind="movie",
            year=None,
            genres=[],
            keywords=[],
            runtime_minutes=runtime,
            popularity=None,
            overview=None,
            similarity=0.5,
        )
        for name, runtime in [("A", 90), ("B", 120)]
    ]
    provider = FakeMetadataProvider(titles={})
    result = _service(db_session)._enrich_runtimes(candidates, provider)
    assert result is candidates  # same list returned untouched
    assert provider.calls == []  # nothing fetched


def test_enrich_runtimes_also_heals_missing_quality_signal(db_session: Session) -> None:
    # The per-title fetch already carries vote_average / vote_count, so while filling a missing
    # runtime we heal a NULL quality signal too (most broad-imported titles had a NULL vote_average,
    # which disabled the re-ranker's quality floor). Never clobber a value the row already has.
    from phare.providers.fakes import FakeMetadataProvider
    from phare.providers.types import TitleMetadata
    from phare.recommend.schema import Candidate

    missing_quality = Title(kind=TitleKind.movie, title="Ungraded", tmdb_id=3001)
    already_graded = Title(
        kind=TitleKind.movie, title="Graded", tmdb_id=3002, vote_average=8.0, vote_count=5_000
    )
    db_session.add_all([missing_quality, already_graded])
    db_session.flush()
    provider = FakeMetadataProvider(
        titles={
            (3001, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie,
                title="Ungraded",
                runtime_minutes=100,
                vote_average=6.5,
                vote_count=1_100,
            ),
            (3002, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie,
                title="Graded",
                runtime_minutes=100,
                vote_average=2.0,  # a re-fetch must NOT overwrite the row's existing 8.0
                vote_count=1,
            ),
        }
    )

    def cand(title: Title) -> Candidate:
        return Candidate(
            title_id=title.id,
            title=title.title,
            kind="movie",
            year=None,
            genres=[],
            keywords=[],
            runtime_minutes=None,  # both need a runtime, so both get fetched
            popularity=None,
            overview=None,
            similarity=0.5,
        )

    _service(db_session)._enrich_runtimes([cand(missing_quality), cand(already_graded)], provider)

    assert missing_quality.vote_average == 6.5  # healed from the fetch
    assert missing_quality.vote_count == 1_100
    assert already_graded.vote_average == 8.0  # untouched — never clobber an existing value
    assert already_graded.vote_count == 5_000


def _runtime_cand(title: Title, runtime: int | None):
    from phare.recommend.schema import Candidate

    return Candidate(
        title_id=title.id,
        title=title.title,
        kind="movie",
        year=None,
        genres=[],
        keywords=[],
        runtime_minutes=runtime,
        popularity=None,
        overview=None,
        similarity=0.5,
    )


def test_runtime_enrichment_persists_so_replay_makes_no_calls(db_session: Session) -> None:
    # C4 validation: a runtime-filtered pool of titles with no runtime fetches + persists them; the
    # same query replayed (its candidates now carry the persisted runtime) fetches nothing.
    from phare.providers.fakes import FakeMetadataProvider
    from phare.providers.types import TitleMetadata

    titles = []
    for i in range(4):
        title = Title(kind=TitleKind.movie, title=f"Film {i}", tmdb_id=3000 + i)
        db_session.add(title)
        titles.append(title)
    db_session.flush()
    meta = {
        (t.tmdb_id, TitleKind.movie): TitleMetadata(
            kind=TitleKind.movie, title=t.title, runtime_minutes=100
        )
        for t in titles
    }
    service = _service(db_session)

    first = FakeMetadataProvider(titles=meta)
    service._enrich_runtimes([_runtime_cand(t, None) for t in titles], first)
    assert len(first.calls) == 4  # every missing runtime fetched...
    assert all(t.runtime_minutes == 100 for t in titles)  # ...and persisted to the titles

    # Replay: candidates rebuilt from the persisted titles carry runtimes → nothing to fetch.
    replay = FakeMetadataProvider(titles=meta)
    service._enrich_runtimes([_runtime_cand(t, t.runtime_minutes) for t in titles], replay)
    assert replay.calls == []  # zero TMDB calls on the second run


def test_runtime_enrichment_is_bounded_per_request(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C4 validation: online work is capped at READ_RUNTIME_CAP per request, so a big under-filled
    # pool can't fan out one TMDB call per title on a single turn (it heals over successive turns).
    from phare.providers.fakes import FakeMetadataProvider
    from phare.providers.types import TitleMetadata

    monkeypatch.setattr("phare.recommend.service.READ_RUNTIME_CAP", 2)
    titles = []
    for i in range(5):
        title = Title(kind=TitleKind.movie, title=f"Film {i}", tmdb_id=4000 + i)
        db_session.add(title)
        titles.append(title)
    db_session.flush()
    provider = FakeMetadataProvider(
        titles={
            (t.tmdb_id, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie, title=t.title, runtime_minutes=100
            )
            for t in titles
        }
    )

    _service(db_session)._enrich_runtimes([_runtime_cand(t, None) for t in titles], provider)

    assert len(provider.calls) == 2  # capped, not one per missing title
    assert sum(t.runtime_minutes == 100 for t in titles) == 2  # only the capped subset persisted


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


def _popular_title(
    session: Session, tmdb_id: int, name: str, genres: list[str], pop: float
) -> None:
    session.add(
        Title(kind=TitleKind.movie, tmdb_id=tmdb_id, title=name, genres=genres, popularity=pop)
    )


def test_popular_row_excludes_hard_avoids(db_session: Session) -> None:
    from phare.recommend.rows import popular_row

    profile = Profile(display_name="anti-comedy")
    db_session.add(profile)
    db_session.flush()
    # Five popular titles, two of them Comedy — a persona that hard-avoids Comedy must see neither.
    _popular_title(db_session, 9001, "Big Drama", ["Drama"], 500.0)
    _popular_title(db_session, 9002, "Silly Comedy", ["Comedy"], 400.0)
    _popular_title(db_session, 9003, "Thrill Ride", ["Thriller"], 300.0)
    _popular_title(db_session, 9004, "Another Comedy", ["Comedy"], 200.0)
    _popular_title(db_session, 9005, "War Epic", ["War"], 100.0)
    db_session.flush()

    row = popular_row(db_session, profile.id, limit=12, hard_avoids=["Comedy"])
    names = {item.title for item in row.items}
    assert "Silly Comedy" not in names and "Another Comedy" not in names
    assert {"Big Drama", "Thrill Ride", "War Epic"} <= names


def test_popular_row_unchanged_without_hard_avoids(db_session: Session) -> None:
    # Anti-regression: no hard-avoids -> the row is exactly the pre-M10.1 popularity-sorted top-N.
    from phare.recommend.rows import popular_row

    profile = Profile(display_name="no-avoids")
    db_session.add(profile)
    db_session.flush()
    _popular_title(db_session, 9101, "Big Drama", ["Drama"], 500.0)
    _popular_title(db_session, 9102, "Silly Comedy", ["Comedy"], 400.0)
    db_session.flush()

    row = popular_row(db_session, profile.id, limit=12)
    assert [item.title for item in row.items] == ["Big Drama", "Silly Comedy"]


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


def test_mood_biases_the_embedding_query(db_session: Session) -> None:
    # A4: an ephemeral chat mood must actually reach retrieval. It's embedded and blended into the
    # taste centroid (one embed call, no completion) — so the mood text shows up as an embed input.
    profile_id = _seeded_profile(db_session)
    fake = FakeLLMProvider()
    service = RecommendationService(
        db_session, embed_provider=fake, embed_model_version="test-embed-v1", chat_llm=None
    )
    service.ensure_embeddings()  # embed the catalog under the (non-local) test version

    base = len(fake.embed_inputs)
    service.recommend(profile_id, mood="slow-burn atmospheric sci-fi", vote_mix=True)
    embedded = [text for call in fake.embed_inputs[base:] for text in call]
    assert "slow-burn atmospheric sci-fi" in embedded  # the mood reached the embedding query

    base2 = len(fake.embed_inputs)
    service.recommend(profile_id, vote_mix=True)  # same turn, no mood
    embedded2 = [text for call in fake.embed_inputs[base2:] for text in call]
    assert "slow-burn atmospheric sci-fi" not in embedded2  # unchanged without a mood


def test_mood_is_ignored_on_the_local_hash_embedder(db_session: Session) -> None:
    # Offline (local hash) vectors carry no meaning, so blending a mood would only add noise — skip
    # it. (Fake embedder pinned to the local version so we can inspect its inputs.)
    profile_id = _seeded_profile(db_session)
    fake = FakeLLMProvider()
    service = RecommendationService(
        db_session, embed_provider=fake, embed_model_version=LOCAL_MODEL_VERSION, chat_llm=None
    )
    service.ensure_embeddings()
    base = len(fake.embed_inputs)
    service.recommend(profile_id, mood="something cosy", vote_mix=True)
    embedded = [text for call in fake.embed_inputs[base:] for text in call]
    assert "something cosy" not in embedded  # guarded off on the local-hash version


def test_appearance_budget_caps_a_title_at_two_per_page() -> None:
    # A7: the same title across three rows is kept in the first two (priority order), dropped from
    # the third.
    from phare.recommend.schema import Recommendation, Row
    from phare.recommend.service import _apply_appearance_budget

    tid = uuid.uuid4()

    def _row(key: str) -> Row:
        item = Recommendation(
            title_id=tid, title="X", kind="movie", year=2020, genres=[], score=1.0
        )
        return Row(key=key, title=key, items=[item])

    out = _apply_appearance_budget([_row("a"), _row("b"), _row("c")], 2)
    assert [len(r.items) for r in out] == [1, 1, 0]


def test_page_rows_respect_budget_and_min_size(db_session: Session) -> None:
    # A7: no title appears more than twice across the page. A10: any surviving "because you watched"
    # row has at least the floor of items (a 1-item because-row looks broken).
    from collections import Counter

    profile_id = _seeded_profile(db_session)
    service = _service(db_session)
    service.ensure_embeddings()
    rows = service.rows(profile_id)

    counts = Counter(item.title_id for row in rows for item in row.items)
    assert counts and all(c <= 2 for c in counts.values())
    for row in rows:
        if row.key.startswith("because:"):
            assert len(row.items) >= 3


def test_continue_watching_and_watch_again_are_mutually_exclusive(db_session: Session) -> None:
    # A11: a show you're partway through belongs in continue_watching, not also in watch_again — the
    # same series in both rows is a visible contradiction. And both rows' items are flagged watched.
    profile_id = _seeded_profile(db_session)
    by_key = {row.key: row for row in _service(db_session).rows(profile_id)}

    cont = by_key["continue_watching"]
    again = by_key["watch_again"]
    cont_ids = {i.title_id for i in cont.items}
    again_ids = {i.title_id for i in again.items}
    assert cont_ids and again_ids  # both rows present in the sample
    assert not (cont_ids & again_ids)  # no title in both
    assert all(i.watched for i in cont.items) and all(i.watched for i in again.items)


def test_profile_building_flag_reflects_centroid_readiness(db_session: Session) -> None:
    # A12: a profile with history but no embeddings yet has no centroid, so personalised rows are
    # empty — flag it "building" (the UI shows a state + popular fallback) rather than a bare page.
    profile_id = _seeded_profile(db_session)  # has watch history, nothing embedded yet
    assert _service(db_session).profile_building(profile_id) is True
    _service(db_session).ensure_embeddings()  # embed the catalog incl. watched titles
    assert _service(db_session).profile_building(profile_id) is False  # centroid ready now
