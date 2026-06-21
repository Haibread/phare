"""The per-item confidence the heuristic rows now expose — recency decay for "continue watching"
and a log-scaled popularity for "popular". Pure functions, so no DB needed here; row wiring is
covered in test_recommend_engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from phare.recommend.rows import _popularity_confidence, _recency_confidence

_NOW = datetime(2026, 6, 21, tzinfo=UTC)


def test_recency_confidence_is_full_when_just_watched() -> None:
    assert _recency_confidence(_NOW, now=_NOW) == 1.0


def test_recency_confidence_halves_each_half_life() -> None:
    # Half-life is 45 days: ~0.5 at 45 days old, ~0.25 at 90.
    six_weeks = _recency_confidence(_NOW - timedelta(days=45), now=_NOW)
    twelve_weeks = _recency_confidence(_NOW - timedelta(days=90), now=_NOW)
    assert abs(six_weeks - 0.5) < 0.01
    assert abs(twelve_weeks - 0.25) < 0.01


def test_recency_confidence_decays_monotonically() -> None:
    recent = _recency_confidence(_NOW - timedelta(days=3), now=_NOW)
    stale = _recency_confidence(_NOW - timedelta(days=120), now=_NOW)
    assert recent > stale


def test_recency_confidence_handles_missing_and_naive_timestamps() -> None:
    assert _recency_confidence(None, now=_NOW) == 0.1
    naive = datetime(2026, 6, 21)  # noqa: DTZ001 — deliberately naive, treated as UTC
    assert _recency_confidence(naive, now=_NOW) == 1.0


def test_popularity_confidence_grows_with_popularity() -> None:
    assert _popularity_confidence(None) == 0.0
    assert _popularity_confidence(0) == 0.0
    mild = _popularity_confidence(100)
    big = _popularity_confidence(1000)
    assert mild < big
    assert big == 1.0  # log10(1001)/3 caps at the full bar
    assert 0.6 < mild < 0.7  # ~two-thirds — a "worth a try" / "strong fit" boundary


def test_popularity_confidence_is_clamped_to_one() -> None:
    assert _popularity_confidence(50_000) == 1.0
