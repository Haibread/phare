"""Closed-loop conversion — the north-star metric (docs/evaluation.md).

Days after we show a recommendation, the watch-sync tells us whether it landed. This joins the
``recommendation_log`` against later ``watch_event`` rows: of titles shown in the top-K, what
fraction were watched within N days? Free, gold-standard signal — which is why every rec is
logged from day one.

Two honesty guards:
- Only **mature** impressions count (window fully elapsed), so a rec shown yesterday with a
  14-day window doesn't drag the rate down before it had a chance.
- **Swing** picks are reported separately, never folded into the accuracy rate — discovery is
  not judged on conversion (design.md).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from phare.db.models import EventType, RecommendationLog, WatchEvent

# A logged rec "converts" when the profile later engages with that title in any of these ways.
_CONVERSION_TYPES = (
    EventType.watched,
    EventType.rewatched,
    EventType.rated,
    EventType.liked,
)


@dataclass(frozen=True)
class ConversionStats:
    """Top-K conversion within a window. ``rate`` is ``None`` when nothing is measurable yet."""

    shown: int
    converted: int
    rate: float | None
    swing_shown: int
    swing_converted: int
    swing_rate: float | None
    top_k: int
    within_days: int

    @staticmethod
    def _rate(converted: int, shown: int) -> float | None:
        return round(converted / shown, 4) if shown else None


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def format_conversion(stats: ConversionStats, scope: str) -> list[str]:
    """Human-readable summary lines for the CLI."""
    window = f"top-{stats.top_k}, {stats.within_days}d"
    main = f"{_pct(stats.rate)} ({stats.converted}/{stats.shown} matured impressions)"
    swing = f"{_pct(stats.swing_rate)} ({stats.swing_converted}/{stats.swing_shown})"
    return [f"Conversion ({scope}, {window}): {main}", f"  swings (not scored): {swing}"]


def conversion_stats(
    session: Session,
    *,
    profile_id: uuid.UUID | None = None,
    top_k: int = 10,
    within_days: int = 14,
    require_mature: bool = True,
    now: datetime | None = None,
) -> ConversionStats:
    """Compute top-K conversion. ``profile_id=None`` aggregates across all profiles (ops view)."""
    now = now or datetime.now(UTC)
    rl = RecommendationLog
    # A watch event for the same (profile, title) whose effective time falls in the window that
    # opens when we showed the rec. occurred_at is the real watch time; fall back to ingested_at.
    effective = func.coalesce(WatchEvent.occurred_at, WatchEvent.ingested_at)
    converted = exists(
        select(WatchEvent.id).where(
            WatchEvent.profile_id == rl.profile_id,
            WatchEvent.title_id == rl.title_id,
            WatchEvent.type.in_(_CONVERSION_TYPES),
            effective >= rl.shown_at,
            effective <= rl.shown_at + timedelta(days=within_days),
        )
    ).label("converted")

    query = select(rl.is_swing, converted).where(rl.rank < top_k)
    if profile_id is not None:
        query = query.where(rl.profile_id == profile_id)
    if require_mature:
        # Only impressions whose window has fully elapsed are measurable.
        query = query.where(rl.shown_at <= now - timedelta(days=within_days))

    main_shown = main_converted = swing_shown = swing_converted = 0
    for is_swing, did_convert in session.execute(query):
        if is_swing:
            swing_shown += 1
            swing_converted += int(bool(did_convert))
        else:
            main_shown += 1
            main_converted += int(bool(did_convert))

    return ConversionStats(
        shown=main_shown,
        converted=main_converted,
        rate=ConversionStats._rate(main_converted, main_shown),
        swing_shown=swing_shown,
        swing_converted=swing_converted,
        swing_rate=ConversionStats._rate(swing_converted, swing_shown),
        top_k=top_k,
        within_days=within_days,
    )
