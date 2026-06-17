"""Build the *query vector* for candidate generation from a profile's own signal.

"Embeddings rank": the taste centroid is a recency-weighted blend of the embeddings of titles
the profile engaged with — pulled toward what they liked/rewatched, pushed away from what they
abandoned or rated low. No LLM, no cross-user data. The structured taste profile steers later,
in the re-ranker; here we only need a point in embedding space to search around.
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
}

# Half-life for recency decay: a year-old event counts half as much as a fresh one.
_RECENCY_HALF_LIFE_DAYS = 365.0


def event_weight(event_type: EventType, rating: float | None) -> float:
    """Net contribution of one event. Ratings are signed around the genre-neutral midpoint 6.

    A 9/10 is strong positive, a 3/10 strong negative; an unrated 'rated' event is neutral.
    """
    if event_type is EventType.rated:
        if rating is None:
            return 0.0
        return (rating - 6.0) / 4.0  # ~[-1.5, +1.0] across a 1-10 scale
    return _EVENT_WEIGHT.get(event_type, 0.0)


def recency_factor(occurred_at: datetime | None, now: datetime) -> float:
    """Exponential decay in [~0, 1]. Undated events get a mild 0.5 so they still count a little."""
    if occurred_at is None:
        return 0.5
    age_days = max((now - occurred_at).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)


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

    accumulator: list[float] | None = None
    total_abs_weight = 0.0
    for event, embedding in rows:
        rating = float(event.rating) if event.rating is not None else None
        weight = event_weight(event.type, rating) * recency_factor(event.occurred_at, now)
        if weight == 0.0:
            continue
        if accumulator is None:
            accumulator = [0.0] * len(embedding)
        for i, value in enumerate(embedding):
            accumulator[i] += weight * value
        total_abs_weight += abs(weight)

    if accumulator is None or total_abs_weight == 0.0:
        return None
    centroid = [value / total_abs_weight for value in accumulator]
    logger.debug(
        "recommend.centroid", extra={"profile_id": str(profile_id), "signal_events": len(rows)}
    )
    return centroid
