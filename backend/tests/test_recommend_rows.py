"""The recency-decay confidence "continue watching" exposes. Pure function, so no DB needed here.

The "popular" row's confidence is now a real *taste fit* (lot R6b), not a popularity magnitude — it
needs embeddings + a centroid, so its wiring is covered in test_recommend_engine, not here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from phare.recommend.rows import _recency_confidence

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
