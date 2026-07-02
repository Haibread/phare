"""Build the *query vector* for candidate generation from a profile's own signal.

"Embeddings rank": the taste centroid is a signed blend of the embeddings of titles the profile
engaged with — pulled toward what they liked/rewatched, pushed away from what they abandoned or
rated low — with only a *gentle* recency tilt, since this is stable taste (an old favourite still
counts). No LLM, no cross-user data. The structured taste profile steers later, in the re-ranker;
here we only need a point in embedding space to search around.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.db.models import EventType, TitleEmbedding, WatchEvent

logger = logging.getLogger(__name__)

# Per-event-type contribution to the centroid. Abandonment and dislikes are *negative* signal
# (design.md: "the signals others throw away"). Rewatch is the strongest comfort signal.
_EVENT_WEIGHT: dict[EventType, float] = {
    EventType.rewatched: 1.5,
    EventType.liked: 1.0,
    EventType.watched: 0.6,
    EventType.watchlisted: 0.3,
    EventType.disliked: -1.0,
    EventType.abandoned: -1.2,
    EventType.not_interested: -1.0,
}

# Recency is a *gentle tilt*, not a cliff. The centroid is STABLE taste — who you are — so a film
# you loved three years ago (Interstellar) must still pull hard; "what I'm into this week" lives in
# the chat layer, not here. Decay is therefore very slow (8-year half-life) and floored, so an old
# favourite never fades below `_RECENCY_FLOOR` of its weight.
_RECENCY_HALF_LIFE_DAYS = 2920.0
_RECENCY_FLOOR = 0.6

# How long after the last episode a started-but-unrated show is treated as abandoned. We have no
# total-episode count, so "abandoned" can't be proven — this is a deliberately conservative proxy.
_ABANDON_STALE_DAYS = 180.0


def event_weight(event_type: EventType, rating: float | None) -> float:
    """Net contribution of one event. Ratings are signed around the genre-neutral midpoint 6.

    A 9/10 is strong positive, a 3/10 strong negative; an unrated 'rated' event is neutral.
    """
    if event_type is EventType.rated:
        if rating is None:
            return 0.0
        return (rating - 6.0) / 4.0  # ~[-1.5, +1.0] across a 1-10 scale
    return _EVENT_WEIGHT.get(event_type, 0.0)


def collapsed_watch_weight(watched: list[WatchEvent], *, has_rating: bool, now: datetime) -> float:
    """Derive ONE signal from a title's watch events — the "signals others throw away" (design.md).

    These signals are never emitted by any source, so we synthesize them deterministically:

    - **Rewatch** (movies watched ≥2×) → the strongest comfort signal. Repeat watches otherwise
      just pile up as separate ``watched`` rows; here they collapse into one ``rewatched`` weight.
    - **Abandonment** (a show with ≥2 episodes whose last episode is stale and that was *never
      rated*) → negative signal. Conservative on purpose: a rating — high *or* low — is an explicit
      verdict we trust over the heuristic, so a rated show is never inferred-abandoned (otherwise a
      finished, loved show like one rated 10 looks identical to one you bailed on).

    Otherwise it's an ordinary ``watched`` engagement. Recency is applied by the caller using the
    most-recent watch.
    """
    is_episode = any(e.episode_id is not None for e in watched)
    if is_episode:
        distinct_episodes = {e.episode_id for e in watched}
        dates = [e.occurred_at for e in watched if e.occurred_at is not None]
        last_watch = max(dates, default=None)
        stale = last_watch is not None and (now - last_watch).days >= _ABANDON_STALE_DAYS
        if len(distinct_episodes) >= 2 and stale and not has_rating:
            return _EVENT_WEIGHT[EventType.abandoned]
        return _EVENT_WEIGHT[EventType.watched]
    if len(watched) >= 2:
        return _EVENT_WEIGHT[EventType.rewatched]
    return _EVENT_WEIGHT[EventType.watched]


def recency_factor(occurred_at: datetime | None, now: datetime) -> float:
    """Gentle recency tilt in [`_RECENCY_FLOOR`, 1]. Fresh engagement counts fully; an old one keeps
    at least the floor, so a long-ago favourite is never erased. Undated events get the floor."""
    if occurred_at is None:
        return _RECENCY_FLOOR
    age_days = max((now - occurred_at).total_seconds() / 86400.0, 0.0)
    return max(_RECENCY_FLOOR, 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS))


def watched_title_ids(session: Session, profile_id: uuid.UUID) -> set[uuid.UUID]:
    """Every title the profile has any event for — these are excluded from candidates."""
    rows = session.scalars(
        select(WatchEvent.title_id).where(WatchEvent.profile_id == profile_id).distinct()
    ).all()
    return set(rows)


def compute_taste_centroid(
    session: Session,
    profile_id: uuid.UUID,
    model_version: str,
    *,
    now: datetime | None = None,
) -> list[float] | None:
    """Recency-weighted, signed blend of engaged-title embeddings. ``None`` if no usable signal."""
    now = now or datetime.now(UTC)
    rows = session.execute(
        select(WatchEvent, TitleEmbedding.embedding)
        .join(TitleEmbedding, TitleEmbedding.title_id == WatchEvent.title_id)
        .where(
            WatchEvent.profile_id == profile_id,
            WatchEvent.excluded.is_(False),
            TitleEmbedding.model_version == model_version,
        )
    ).all()

    if not rows:
        return None

    # Group events by title so watched events can collapse into one derived rewatch/abandon signal
    # (a title's embedding is the same across all its events).
    by_title: dict[uuid.UUID, tuple[list[float], list[WatchEvent]]] = {}
    for event, embedding in rows:
        by_title.setdefault(event.title_id, (embedding, []))[1].append(event)

    # Each contribution is (weight, occurred_at) against the title's embedding.
    accumulator: list[float] | None = None
    total_abs_weight = 0.0

    def add(weight: float, occurred_at: datetime | None, embedding: list[float]) -> None:
        nonlocal accumulator, total_abs_weight
        weight *= recency_factor(occurred_at, now)
        if weight == 0.0:
            return
        if accumulator is None:
            accumulator = [0.0] * len(embedding)
        for i, value in enumerate(embedding):
            accumulator[i] += weight * value
        total_abs_weight += abs(weight)

    for embedding, events in by_title.values():
        watched = [e for e in events if e.type is EventType.watched]
        rated = [e for e in events if e.type is EventType.rated]
        # Watched events collapse into a single rewatch/abandon/engagement signal, dated by the
        # most recent watch.
        if watched:
            last_watch = max((e.occurred_at for e in watched if e.occurred_at), default=None)
            weight = collapsed_watch_weight(watched, has_rating=bool(rated), now=now)
            add(weight, last_watch, embedding)
        # Ratings and everything else (watchlist, …) stay per-event — explicit, distinct signals.
        for event in events:
            if event.type is EventType.watched:
                continue
            rating = float(event.rating) if event.rating is not None else None
            add(event_weight(event.type, rating), event.occurred_at, embedding)

    if accumulator is None or total_abs_weight == 0.0:
        return None
    centroid = [value / total_abs_weight for value in accumulator]
    logger.debug(
        "recommend.centroid", extra={"profile_id": str(profile_id), "signal_events": len(rows)}
    )
    return centroid
