"""Synthesized negative/comfort signals: rewatch + abandonment derivation, hard-avoid matching.

These signals are never emitted by any source (Trakt/Plex/Jellyfin only produce watched/rated/
watchlisted), so the engine derives them deterministically here. See docs/data-model.md.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.db.models import EventType, Profile, Title, TitleEmbedding, TitleKind, WatchEvent
from phare.embeddings.service import EmbeddingService
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.recommend.candidates import _is_hard_avoided
from phare.recommend.taste_vector import collapsed_watch_weight, compute_taste_centroid

_NOW = datetime(2026, 6, 20, tzinfo=UTC)


def _watch(*, episode_id: uuid.UUID | None, days_ago: int) -> WatchEvent:
    return WatchEvent(
        type=EventType.watched,
        episode_id=episode_id,
        occurred_at=_NOW - timedelta(days=days_ago),
    )


# --- derivation logic (pure) -------------------------------------------------


def test_recency_is_a_gentle_tilt_not_a_cliff() -> None:
    from phare.recommend.taste_vector import _RECENCY_FLOOR, recency_factor

    fresh = recency_factor(_NOW, now=_NOW)
    three_years = recency_factor(_NOW - timedelta(days=365 * 3), now=_NOW)
    ancient = recency_factor(_NOW - timedelta(days=365 * 15), now=_NOW)

    assert fresh == 1.0
    assert three_years > 0.7  # a film loved 3 years ago still counts strongly, not ~12%
    assert ancient == _RECENCY_FLOOR  # floored — an old favourite is never erased
    assert recency_factor(None, now=_NOW) == _RECENCY_FLOOR  # undated events get the floor


def test_single_movie_watch_is_ordinary_engagement() -> None:
    weight = collapsed_watch_weight(
        [_watch(episode_id=None, days_ago=10)], has_rating=False, now=_NOW
    )
    assert weight == 0.6


def test_movie_watched_twice_is_a_rewatch() -> None:
    watched = [_watch(episode_id=None, days_ago=400), _watch(episode_id=None, days_ago=10)]
    assert collapsed_watch_weight(watched, has_rating=False, now=_NOW) == 1.5  # rewatched comfort


def test_started_stale_unrated_show_is_abandoned() -> None:
    eps = [_watch(episode_id=uuid.uuid4(), days_ago=300) for _ in range(2)]
    assert collapsed_watch_weight(eps, has_rating=False, now=_NOW) == -1.2  # abandoned


def test_rated_show_is_never_inferred_abandoned() -> None:
    # A rating — high or low — is an explicit verdict we trust over the heuristic, so a finished,
    # loved show (rated 10) is not mistaken for an abandoned one.
    eps = [_watch(episode_id=uuid.uuid4(), days_ago=300) for _ in range(2)]
    assert collapsed_watch_weight(eps, has_rating=True, now=_NOW) == 0.6


def test_recent_show_is_not_abandoned() -> None:
    eps = [_watch(episode_id=uuid.uuid4(), days_ago=5) for _ in range(2)]
    assert collapsed_watch_weight(eps, has_rating=False, now=_NOW) == 0.6


def test_single_episode_is_not_abandoned() -> None:
    eps = [_watch(episode_id=uuid.uuid4(), days_ago=300)]
    assert collapsed_watch_weight(eps, has_rating=False, now=_NOW) == 0.6


# --- hard-avoid matching (C3) ------------------------------------------------


def _title(
    name: str, *, genres: list[str] | None = None, keywords: list[str] | None = None
) -> Title:
    return Title(kind=TitleKind.movie, title=name, genres=genres or [], keywords=keywords or [])


def test_hard_avoid_matches_on_word_boundaries_not_substrings() -> None:
    assert _is_hard_avoided(_title("Warrior"), ["war"]) is False  # no longer over-filters
    assert _is_hard_avoided(_title("Steward of Gondor"), ["war"]) is False
    assert _is_hard_avoided(_title("Apocalypse Now", genres=["War"]), ["war"]) is True
    assert (
        _is_hard_avoided(_title("Making a Murderer", keywords=["true crime"]), ["true crime"])
        is True
    )


# --- end-to-end: the derived weight actually shifts the centroid --------------


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb)


def test_rewatched_movie_pulls_the_centroid_harder(db_session: Session) -> None:
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()
    profile_id = profile.id
    once = Title(kind=TitleKind.movie, tmdb_id=1, title="Once", genres=[], keywords=[])
    twice = Title(kind=TitleKind.movie, tmdb_id=2, title="Twice", genres=[], keywords=[])
    db_session.add_all([once, twice])
    db_session.flush()

    def watch(title: Title, ref: str) -> None:
        db_session.add(
            WatchEvent(
                profile_id=profile_id,
                title_id=title.id,
                type=EventType.watched,
                occurred_at=_NOW - timedelta(days=10),
                source="t",
                external_ref=ref,
            )
        )

    watch(once, "o1")
    watch(twice, "t1")
    watch(twice, "t2")  # rewatch -> stronger comfort weight
    db_session.flush()
    EmbeddingService(db_session, LocalHashEmbeddingProvider(), LOCAL_MODEL_VERSION).embed_missing()

    centroid = compute_taste_centroid(db_session, profile_id, LOCAL_MODEL_VERSION, now=_NOW)
    assert centroid is not None

    def embedding_of(title: Title) -> list[float]:
        vec = db_session.scalar(
            select(TitleEmbedding.embedding).where(TitleEmbedding.title_id == title.id)
        )
        assert vec is not None
        return list(vec)

    # The rewatched title sits closer to the taste centroid than the once-watched one.
    assert _cosine(centroid, embedding_of(twice)) > _cosine(centroid, embedding_of(once))
