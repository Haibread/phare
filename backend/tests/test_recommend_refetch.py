"""Adaptive constraint-aware re-fetch (round-7 finding 1).

A taste centred on one cluster (thrillers) starves a chat turn that asks for a different genre
(comedy): the ~nearest-to-centroid candidate slice contains almost no comedies, so the post-hoc
genre/runtime filter reduces the slate to a handful, then to ~1. The fix re-runs the ANN with the
constraint pushed into SQL, searching the *matching* subspace. These tests drive the real pgvector
path with hand-built embeddings so the centroid geometry is deterministic.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from phare.db.models import (
    EMBEDDING_DIM,
    EventType,
    Profile,
    Title,
    TitleEmbedding,
    TitleKind,
    WatchEvent,
)
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.recommend.service import RecommendationService

# Two orthogonal directions in embedding space: a taste centred on "thriller" is cosine-distant from
# every "comedy" title, so the taste-nearest slice contains no comedies until the filter forces a
# subspace re-search.
_THRILLER = [1.0, 0.0] + [0.0] * (EMBEDDING_DIM - 2)
_COMEDY = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)


def _service(session: Session) -> RecommendationService:
    return RecommendationService(
        session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=None,
        row_size=12,
    )


def _add_title(
    session: Session,
    *,
    tmdb_id: int,
    genres: list[str],
    vector: list[float],
    runtime: int | None,
    vote_count: int = 3_000,
    vote_average: float = 7.5,
    language: str | None = None,
) -> Title:
    title = Title(
        kind=TitleKind.movie,
        tmdb_id=tmdb_id,
        title=f"Title {tmdb_id}",
        genres=genres,
        runtime_minutes=runtime,
        vote_count=vote_count,
        vote_average=vote_average,
        original_language=language,
        overview="A candidate.",
    )
    session.add(title)
    session.flush()
    session.add(
        TitleEmbedding(title_id=title.id, model_version=LOCAL_MODEL_VERSION, embedding=vector)
    )
    return title


def _thriller_taste_profile(session: Session, *, comedies: int, comedy_runtime: int) -> uuid.UUID:
    """A profile whose taste centroid points at the thriller cluster, over a catalog of many
    thrillers plus ``comedies`` short comedies far from the centroid."""
    profile = Profile(display_name="thriller fan")
    session.add(profile)
    session.flush()

    # One watched thriller anchors the centroid on the thriller direction.
    watched = _add_title(session, tmdb_id=1000, genres=["Thriller"], vector=_THRILLER, runtime=140)
    session.add(
        WatchEvent(
            profile_id=profile.id, title_id=watched.id, type=EventType.watched, source="test"
        )
    )
    # A dense thriller cluster near the centroid — these dominate the unconstrained nearest slice.
    for i in range(40):
        _add_title(session, tmdb_id=2000 + i, genres=["Thriller"], vector=_THRILLER, runtime=150)
    # A handful of short comedies, cosine-distant from the thriller centroid.
    for i in range(comedies):
        _add_title(
            session,
            tmdb_id=3000 + i,
            genres=["Comedy"],
            vector=_COMEDY,
            runtime=comedy_runtime,
        )
    session.flush()
    return profile.id


def test_refetch_fills_a_comedy_slate_a_thriller_taste_would_starve(db_session: Session) -> None:
    # 15 short comedies exist, but they're far from the thriller centroid. Without the re-fetch the
    # nearest slice is all thrillers → the comedy filter leaves ~0. The SQL-side re-fetch searches
    # the comedy subspace and returns a full slate.
    profile_id = _thriller_taste_profile(db_session, comedies=15, comedy_runtime=95)
    service = _service(db_session)

    def comedy_only(candidates: list) -> list:
        return [c for c in candidates if "Comedy" in c.genres]

    items = service.recommend(
        profile_id,
        candidate_filter=comedy_only,
        include_genres=["comedy"],
        max_runtime=105,
        vote_mix=True,
    )
    assert len(items) >= 10  # a real slate, not the 1-item starve
    # And they really are the requested subspace — the re-fetch didn't just pad with thrillers.
    fetched = {i.title_id for i in items}
    comedy_ids = {
        t.id
        for t in db_session.query(Title).filter(Title.genres.any("Comedy")).all()  # type: ignore[attr-defined]
    }
    assert fetched <= comedy_ids


def test_honest_thin_result_stays_thin_when_catalog_has_few_matches(db_session: Session) -> None:
    # Only 2 comedies in the whole catalog: the re-fetch fires but can't manufacture titles that
    # aren't there. An honest thin slate, never padded with thrillers (principle 4).
    profile_id = _thriller_taste_profile(db_session, comedies=2, comedy_runtime=95)
    service = _service(db_session)

    def comedy_only(candidates: list) -> list:
        return [c for c in candidates if "Comedy" in c.genres]

    items = service.recommend(
        profile_id,
        candidate_filter=comedy_only,
        include_genres=["comedy"],
        max_runtime=105,
        vote_mix=True,
    )
    assert len(items) <= 2  # thin and honest
    # Not a single thriller snuck in as padding.
    for item in items:
        assert item.genres == ["Comedy"]


def test_no_refetch_when_first_pass_already_full(db_session: Session) -> None:
    # A comedy request against a comedy-heavy nearest slice is satisfied on the first pass; the
    # re-fetch path shouldn't be needed (and the result is a full comedy slate regardless).
    profile = Profile(display_name="comedy fan")
    db_session.add(profile)
    db_session.flush()
    watched = _add_title(db_session, tmdb_id=500, genres=["Comedy"], vector=_COMEDY, runtime=100)
    db_session.add(
        WatchEvent(
            profile_id=profile.id, title_id=watched.id, type=EventType.watched, source="test"
        )
    )
    for i in range(30):
        _add_title(db_session, tmdb_id=600 + i, genres=["Comedy"], vector=_COMEDY, runtime=100)
    db_session.flush()

    service = _service(db_session)

    def comedy_only(candidates: list) -> list:
        return [c for c in candidates if "Comedy" in c.genres]

    items = service.recommend(
        profile.id, candidate_filter=comedy_only, include_genres=["comedy"], vote_mix=True
    )
    assert len(items) >= 10


def _batman_taste_with_animation(
    db_session: Session, *, western_language: str | None, anime_language: str | None
) -> uuid.UUID:
    """The coordinator's live case, in miniature: a profile whose taste sits on a cluster of
    western (DC-style) animation, with a handful of Japanese anime far from the centroid. An
    "anime" ask must cross to the ja subspace, not serve the nearby western animation."""
    profile = Profile(display_name="batman fan")
    db_session.add(profile)
    db_session.flush()
    watched = _add_title(
        db_session,
        tmdb_id=7000,
        genres=["Action"],
        vector=_THRILLER,
        runtime=140,
        language=western_language,
    )
    db_session.add(
        WatchEvent(
            profile_id=profile.id, title_id=watched.id, type=EventType.watched, source="test"
        )
    )
    # Western animation ON the taste direction — these dominate the unconstrained nearest slice
    # (the DC animated movies the live validation surfaced).
    for i in range(20):
        _add_title(
            db_session,
            tmdb_id=7100 + i,
            genres=["Animation"],
            vector=_THRILLER,
            runtime=90,
            language=western_language,
        )
    # Japanese anime, cosine-distant from the centroid.
    for i in range(8):
        _add_title(
            db_session,
            tmdb_id=7200 + i,
            genres=["Animation"],
            vector=_COMEDY,
            runtime=100,
            language=anime_language,
        )
    db_session.flush()
    return profile.id


def test_anime_intent_refetches_japanese_animation_only(db_session: Session) -> None:
    # Healed catalog (languages known): "anime" = Animation + ja in BOTH layers. The in-memory
    # filter rejects the nearby western animation (its zero-match fallback would keep the pool, but
    # the genre-starvation check still fires the SQL re-fetch), and the SQL re-fetch searches the
    # Animation+ja subspace — so the slate is exactly the Japanese titles, never DC animation.
    from phare.agent.schema import ChatIntent
    from phare.agent.service import intent_filter

    profile_id = _batman_taste_with_animation(
        db_session, western_language="en", anime_language="ja"
    )
    intent = ChatIntent(include_genres=["anime"])

    items = _service(db_session).recommend(
        profile_id,
        candidate_filter=intent_filter(intent),
        include_genres=intent.include_genres,
        vote_mix=True,
    )
    assert items  # the ja subspace was found
    ja_ids = {t.id for t in db_session.query(Title).filter(Title.original_language == "ja").all()}
    assert {i.title_id for i in items} <= ja_ids  # every pick is Japanese Animation
    assert all("Animation" in i.genres for i in items)


def test_anime_intent_on_preheal_catalog_downgrades_to_animation_and_records(
    db_session: Session, caplog
) -> None:
    # Pre-heal catalog: every original_language is NULL, so the ja condition is unenforceable.
    # The ask degrades to plain Animation — visibly (anime_language_unknown recorded), never
    # silently — so chat stays useful before the boot heal has run.
    import logging

    from phare.agent.schema import ChatIntent
    from phare.agent.service import intent_filter

    profile_id = _batman_taste_with_animation(
        db_session, western_language=None, anime_language=None
    )
    intent = ChatIntent(include_genres=["anime"])

    with caplog.at_level(logging.WARNING, logger="phare.fallback"):
        items = _service(db_session).recommend(
            profile_id,
            candidate_filter=intent_filter(intent),
            include_genres=intent.include_genres,
            vote_mix=True,
        )
    assert items
    assert all("Animation" in i.genres for i in items)  # the genre half still holds
    assert any(
        r.name == "phare.fallback" and getattr(r, "reason", "") == "anime_language_unknown"
        for r in caplog.records
    )


def test_plain_animation_intent_spans_all_languages(db_session: Session) -> None:
    # "animation" is NOT origin-scoped: on a healed catalog the slate may carry both western and
    # Japanese animation — language never filters a plain genre ask.
    from phare.agent.schema import ChatIntent
    from phare.agent.service import intent_filter

    profile_id = _batman_taste_with_animation(
        db_session, western_language="en", anime_language="ja"
    )
    intent = ChatIntent(include_genres=["animation"])

    items = _service(db_session).recommend(
        profile_id,
        candidate_filter=intent_filter(intent),
        include_genres=intent.include_genres,
        vote_mix=True,
    )
    assert items
    assert all("Animation" in i.genres for i in items)
    # The nearest slice is the western cluster — a plain ask is satisfied there, no ja demand.
    western_ids = {
        t.id for t in db_session.query(Title).filter(Title.original_language == "en").all()
    }
    assert {i.title_id for i in items} & western_ids  # western animation allowed through


def test_generate_candidates_applies_origin_language_in_sql(db_session: Session) -> None:
    # Pin the SQL WHERE itself: a GenreConstraint carrying an origin language returns only rows
    # whose genres overlap AND whose original_language matches — NULL-language rows excluded.
    from phare.recommend import genres as genre_mod
    from phare.recommend.candidates import generate_candidates

    profile = Profile(display_name="direct")
    db_session.add(profile)
    db_session.flush()
    ja = _add_title(
        db_session, tmdb_id=7500, genres=["Animation"], vector=_COMEDY, runtime=100, language="ja"
    )
    _add_title(
        db_session, tmdb_id=7501, genres=["Animation"], vector=_COMEDY, runtime=100, language="en"
    )
    _add_title(
        db_session, tmdb_id=7502, genres=["Animation"], vector=_COMEDY, runtime=100, language=None
    )
    _add_title(
        db_session, tmdb_id=7503, genres=["Drama"], vector=_COMEDY, runtime=100, language="ja"
    )
    db_session.flush()

    candidates = generate_candidates(
        db_session,
        profile.id,
        _COMEDY,
        LOCAL_MODEL_VERSION,
        genre_constraints=[
            genre_mod.GenreConstraint(labels=("Animation",), original_language="ja")
        ],
    )
    assert [c.title_id for c in candidates] == [ja.id]
    assert candidates[0].original_language == "ja"  # carried onto the Candidate for the filter


def test_refetch_resolves_french_genre_alias_to_catalog_label(db_session: Session) -> None:
    # The SQL genre filter honours the alias semantics: a French word ("comédie") resolves to the
    # catalog's English "Comedy" label before the array-overlap filter runs.
    profile_id = _thriller_taste_profile(db_session, comedies=15, comedy_runtime=95)
    service = _service(db_session)

    def comedy_only(candidates: list) -> list:
        return [c for c in candidates if "Comedy" in c.genres]

    items = service.recommend(
        profile_id,
        candidate_filter=comedy_only,
        include_genres=["comédie"],
        vote_mix=True,
    )
    assert len(items) >= 10


def test_runtime_cap_on_a_runtime_poor_pool_does_not_starve_to_cartoons(
    db_session: Session, caplog
) -> None:
    # The live prod regression ("Une série policière intelligente, pas trop longue"): the planner
    # extracts crime + max_runtime=40, the re-fetch finds the right Crime shows — and then the old
    # filter dropped all 20 NULL-runtime crime shows (TV rows almost never carry an episode
    # runtime), leaving only the 3 kids' cartoons with a well-known 22-min episode. The tiered
    # filter must fill the slate from the unknown-runtime crime tier instead, visibly.
    import logging

    from phare.agent.schema import ChatIntent
    from phare.agent.service import intent_filter

    profile = Profile(display_name="crime fan")
    db_session.add(profile)
    db_session.flush()
    watched = _add_title(db_session, tmdb_id=8000, genres=["Crime"], vector=_THRILLER, runtime=50)
    db_session.add(
        WatchEvent(
            profile_id=profile.id, title_id=watched.id, type=EventType.watched, source="test"
        )
    )
    # 20 crime shows near the taste centroid — none with a known runtime (the live TV reality).
    crime_ids = {
        _add_title(
            db_session, tmdb_id=8100 + i, genres=["Crime"], vector=_THRILLER, runtime=None
        ).id
        for i in range(20)
    }
    # 3 kids' cartoons with a known 22-minute episode — the only titles that provably fit cap 40.
    cartoon_ids = {
        _add_title(db_session, tmdb_id=8200 + i, genres=["Crime"], vector=_THRILLER, runtime=22).id
        for i in range(3)
    }
    db_session.flush()

    intent = ChatIntent(include_genres=["crime"], max_runtime=40)
    service = _service(db_session)
    with caplog.at_level(logging.WARNING, logger="phare.fallback"):
        items = service.recommend(
            profile.id,
            candidate_filter=intent_filter(intent, slate_size=service.row_size),
            include_genres=intent.include_genres,
            max_runtime=intent.max_runtime,
            vote_mix=True,
        )

    fetched = {i.title_id for i in items}
    assert not fetched <= cartoon_ids  # NOT 100% Scooby-Doo
    assert len(fetched & crime_ids) >= 5  # the unknown-runtime crime tier fills the slate
    assert len(items) >= 10  # a real slate, not a 3-item strip
    # The degradation is observable — unknown-runtime filler was recorded, not silent.
    assert any(
        r.name == "phare.fallback" and getattr(r, "reason", "") == "runtime_unknown_kept"
        for r in caplog.records
    )
