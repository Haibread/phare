"""Row assembly. ``you_might_like`` (the full pipeline) is built in the service; the simpler,
non-LLM rows live here. Every row degrades to empty rather than erroring at N=0.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy import Float, cast, func, select, true
from sqlalchemy.orm import Session

from phare.core.i18n import DEFAULT_LANGUAGE, Language, translate
from phare.db.models import EventType, Title, TitleKind, WatchEvent
from phare.recommend.schema import Recommendation, Row

_MIN_DT = datetime(1, 1, 1, tzinfo=UTC)

# "Continue watching" confidence decays with how long ago you last touched the show — a thread you
# watched this week is warm, one you abandoned months ago has cooled off. Half-life ~6 weeks.
_CONTINUE_HALF_LIFE_DAYS = 45.0
# "Popular" confidence is the popularity magnitude on a log scale: TMDB popularity ~1000 reads as a
# full bar, a mild ~100 as roughly two-thirds, so a runaway hit outranks a lukewarm one honestly.
_POPULAR_LOG_FULL = 3.0


def _recency_confidence(last: datetime | None, *, now: datetime) -> float:
    """Exponential decay of a "keep watching" signal by days since the last episode."""
    if last is None:
        return 0.1
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    days = max((now - last).total_seconds() / 86_400.0, 0.0)
    return round(0.5 ** (days / _CONTINUE_HALF_LIFE_DAYS), 4)


def _popularity_confidence(popularity: float | None) -> float:
    """Map an unbounded TMDB popularity to a [0, 1] confidence on a log scale."""
    if not popularity or popularity <= 0:
        return 0.0
    return round(min(math.log10(popularity + 1.0) / _POPULAR_LOG_FULL, 1.0), 4)


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
        # Deterministic base order: the interleave below is a *stable* sort, so an arbitrary SQL
        # row order would let ties (equal rating / same recency) pick different seeds run-to-run —
        # cascading into which "because" rows exist and can dedup you_might_like empty. Stable id.
        .order_by(Title.tmdb_id.asc().nulls_last(), Title.id)
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
    title: Title,
    *,
    score: float,
    confidence: float | None,
    explanation: str,
    watched: bool = False,
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
        watched=watched,
    )


def watch_again_row(
    session: Session,
    profile_id: uuid.UUID,
    *,
    limit: int = 12,
    language: Language = DEFAULT_LANGUAGE,
    exclude_ids: Collection[uuid.UUID] = (),
) -> Row:
    """Titles the profile clearly liked — highest rating / liked / rewatched, deduped by title.

    ``exclude_ids`` drops titles already in *continue watching* — a show you're partway through
    belongs there, not in "watch again", so the same series can't sit in both (review A11)."""
    best_rating = func.max(cast(WatchEvent.rating, Float))
    rows = session.execute(
        select(Title, best_rating.label("rating"))
        .join(WatchEvent, WatchEvent.title_id == Title.id)
        .where(
            WatchEvent.profile_id == profile_id,
            WatchEvent.excluded.is_(False),
            Title.id.notin_(exclude_ids) if exclude_ids else true(),
            WatchEvent.type.in_(
                [EventType.rated, EventType.liked, EventType.rewatched, EventType.watched]
            ),
        )
        .group_by(Title.id)
        .having(
            (best_rating >= 7.0)
            | func.bool_or(WatchEvent.type.in_([EventType.liked, EventType.rewatched]))
        )
        .order_by(best_rating.desc().nulls_last(), Title.tmdb_id.asc().nulls_last(), Title.id)
        .limit(limit)
    ).all()
    items = [
        _rec(
            title,
            score=float(rating) if rating is not None else 0.0,
            confidence=min((float(rating) / 10.0) if rating is not None else 0.6, 1.0),
            explanation=translate(language, "explain.watchAgain"),
            watched=True,  # these are titles you've seen and liked
        )
        for title, rating in rows
    ]
    return Row(key="watch_again", title=translate(language, "row.watchAgain"), items=items)


def popular_row(
    session: Session,
    profile_id: uuid.UUID,
    *,
    limit: int = 12,
    language: Language = DEFAULT_LANGUAGE,
) -> Row:
    """Global popularity over the catalog, excluding what the profile has already seen."""
    watched = (
        select(WatchEvent.title_id).where(WatchEvent.profile_id == profile_id).scalar_subquery()
    )
    rows = session.scalars(
        select(Title)
        .where(Title.id.notin_(watched), Title.popularity.isnot(None))
        .order_by(Title.popularity.desc(), Title.tmdb_id.asc().nulls_last(), Title.id)
        .limit(limit)
    ).all()
    items = [
        _rec(
            title,
            score=title.popularity or 0.0,
            confidence=_popularity_confidence(title.popularity),
            explanation=translate(language, "explain.popular"),
        )
        for title in rows
    ]
    return Row(key="popular", title=translate(language, "row.popular"), items=items)


def continue_watching_row(
    session: Session,
    profile_id: uuid.UUID,
    *,
    limit: int = 12,
    language: Language = DEFAULT_LANGUAGE,
) -> Row:
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
        .order_by(last_activity.desc().nulls_last(), Title.tmdb_id.asc().nulls_last(), Title.id)
        .limit(limit)
    ).all()
    now = datetime.now(UTC)
    items = [
        _rec(
            title,
            score=1.0,
            confidence=_recency_confidence(last, now=now),
            explanation=translate(language, "explain.continueWatching"),
            watched=True,  # a show you're partway through — you've seen episodes of it
        )
        for title, last in rows
    ]
    return Row(
        key="continue_watching", title=translate(language, "row.continueWatching"), items=items
    )
