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
) -> Title:
    title = Title(
        kind=TitleKind.movie,
        tmdb_id=tmdb_id,
        title=f"Title {tmdb_id}",
        genres=genres,
        runtime_minutes=runtime,
        vote_count=vote_count,
        vote_average=vote_average,
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
