"""Row assembly. ``you_might_like`` (the full pipeline) is built in the service; the simpler,
non-LLM rows live here. Every row degrades to empty rather than erroring at N=0.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from phare.db.models import EventType, Title, TitleKind, WatchEvent
from phare.recommend.schema import Recommendation, Row

_MIN_DT = datetime(1, 1, 1, tzinfo=UTC)


def loved_seed_titles(session: Session, profile_id: uuid.UUID, *, limit: int = 3) -> list[Title]:
    """Up to ``limit`` titles to anchor "because you watched X" rows — a varied mix of the
    profile's highest-rated / most-loved and most-recently-loved titles."""
    best_rating = func.max(cast(WatchEvent.rating, Float))
    loved = func.bool_or(WatchEvent.type.in_([EventType.liked, EventType.rewatched]))
    recent = func.max(WatchEvent.occurred_at)
    rows = session.execute(
        select(Title, best_rating.label("rating"), loved.label("loved"), recent.label("recent"))
        .join(WatchEvent, WatchEvent.title_id == Title.id)
        .where(
            WatchEvent.profile_id == profile_id,
            WatchEvent.excluded.is_(False),
            WatchEvent.type.in_(
                [EventType.rated, EventType.liked, EventType.rewatched, EventType.watched]
            ),
        )
        .group_by(Title.id)
        .having((best_rating >= 7.0) | loved)
    ).all()
    if not rows:
        return []
    data = [(t, float(r) if r is not None else 6.0, bool(lv), rc) for t, r, lv, rc in rows]
    by_score = sorted(data, key=lambda d: d[1] / 10.0 + (0.25 if d[2] else 0.0), reverse=True)
    by_recent = sorted(data, key=lambda d: (d[3] is not None, d[3] or _MIN_DT), reverse=True)

    seeds: list[Title] = []
    seen: set[uuid.UUID] = set()
    # Interleave top-by-score and top-by-recency for variety, deduped, capped at `limit`.
    for i in range(max(len(by_score), len(by_recent))):
        for ranked in (by_score, by_recent):
            if i < len(ranked) and ranked[i][0].id not in seen:
                seen.add(ranked[i][0].id)
                seeds.append(ranked[i][0])
        if len(seeds) >= limit:
            break
    return seeds[:limit]


def _rec(
    title: Title, *, score: float, confidence: float | None, explanation: str
) -> Recommendation:
    return Recommendation(
        title_id=title.id,
        title=title.title,
        kind=title.kind.value,
        year=title.year,
        genres=list(title.genres),
        score=round(score, 4),
        confidence=confidence,
        explanation=explanation,
        poster_path=title.poster_path,
    )


def watch_again_row(session: Session, profile_id: uuid.UUID, *, limit: int = 12) -> Row:
    """Titles the profile clearly liked — highest rating / liked / rewatched, deduped by title."""
    best_rating = func.max(cast(WatchEvent.rating, Float))
    rows = session.execute(
        select(Title, best_rating.label("rating"))
        .join(WatchEvent, WatchEvent.title_id == Title.id)
        .where(
            WatchEvent.profile_id == profile_id,
            WatchEvent.excluded.is_(False),
            WatchEvent.type.in_(
                [EventType.rated, EventType.liked, EventType.rewatched, EventType.watched]
            ),
        )
        .group_by(Title.id)
        .having(
            (best_rating >= 7.0)
            | func.bool_or(WatchEvent.type.in_([EventType.liked, EventType.rewatched]))
        )
        .order_by(best_rating.desc().nulls_last())
        .limit(limit)
    ).all()
    items = [
        _rec(
            title,
            score=float(rating) if rating is not None else 0.0,
            confidence=min((float(rating) / 10.0) if rating is not None else 0.6, 1.0),
            explanation="You rated this highly — worth another night.",
        )
        for title, rating in rows
    ]
    return Row(key="watch_again", title="Watch again", items=items)


def popular_row(session: Session, profile_id: uuid.UUID, *, limit: int = 12) -> Row:
    """Global popularity over the catalog, excluding what the profile has already seen."""
    watched = (
        select(WatchEvent.title_id).where(WatchEvent.profile_id == profile_id).scalar_subquery()
    )
    rows = session.scalars(
        select(Title)
        .where(Title.id.notin_(watched), Title.popularity.isnot(None))
        .order_by(Title.popularity.desc())
        .limit(limit)
    ).all()
    items = [
        _rec(
            title, score=title.popularity or 0.0, confidence=None, explanation="Popular right now."
        )
        for title in rows
    ]
    return Row(key="popular", title="Popular", items=items)


def continue_watching_row(session: Session, profile_id: uuid.UUID, *, limit: int = 12) -> Row:
    """Shows the profile is partway through (has episode-level history for), most recent first."""
    last_activity = func.max(WatchEvent.occurred_at)
    rows = session.execute(
        select(Title, last_activity.label("last"))
        .join(WatchEvent, WatchEvent.title_id == Title.id)
        .where(
            WatchEvent.profile_id == profile_id,
            WatchEvent.excluded.is_(False),
            WatchEvent.episode_id.isnot(None),
            Title.kind == TitleKind.show,
        )
        .group_by(Title.id)
        .order_by(last_activity.desc().nulls_last())
        .limit(limit)
    ).all()
    items = [
        _rec(title, score=1.0, confidence=None, explanation="Pick up where you left off.")
        for title, _ in rows
    ]
    return Row(key="continue_watching", title="Continue watching", items=items)
