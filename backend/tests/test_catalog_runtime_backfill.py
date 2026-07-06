"""Bulk background runtime heal (round-7 finding 2).

The broad discover import omits runtime, so a runtime cap filters on a ghost catalog. At boot, when
most titles are runtime-less, a background thread walks the NULL-runtime rows and fetches each
title's TMDB detail. These tests drive the "background" work synchronously (the real scheduler
spawns a daemon thread with its own DB connection a test can't roll back), and never hit a network.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

import phare.catalog.runtime_backfill as rb
from phare.catalog.bootstrap import _schedule_runtime_heal_if_gapped
from phare.catalog.runtime_backfill import (
    runtime_backfill_running,
    runtime_gap,
    schedule_runtime_backfill,
)
from phare.core.config import Settings
from phare.db.models import Title, TitleKind
from phare.providers.fakes import FakeMetadataProvider
from phare.providers.types import TitleMetadata


@pytest.fixture(autouse=True)
def _reset_flag() -> None:
    yield
    with rb._lock:
        rb._running = False


def _add(session: Session, *, tmdb_id: int, runtime: int | None, **extra: object) -> Title:
    title = Title(
        kind=TitleKind.movie, tmdb_id=tmdb_id, title=f"T{tmdb_id}", runtime_minutes=runtime, **extra
    )
    session.add(title)
    return title


def test_runtime_gap_is_share_of_null_runtimes(db_session: Session) -> None:
    assert runtime_gap(db_session) == 0.0  # empty catalog
    for i in range(3):
        _add(db_session, tmdb_id=1000 + i, runtime=None)
    _add(db_session, tmdb_id=2000, runtime=100)
    db_session.flush()
    assert runtime_gap(db_session) == 0.75  # 3 of 4 missing


def test_run_backfill_fills_null_runtimes_and_heals_quality_and_is_idempotent(
    db_session: Session,
) -> None:
    titles = [_add(db_session, tmdb_id=3000 + i, runtime=None, vote_count=500) for i in range(3)]
    graded = _add(db_session, tmdb_id=3100, runtime=90, vote_average=8.0)  # already has runtime
    db_session.flush()
    provider = FakeMetadataProvider(
        titles={
            (t.tmdb_id, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie,
                title=t.title,
                runtime_minutes=100 + i,
                vote_average=6.5,
                vote_count=1_200,
            )
            for i, t in enumerate(titles)
        }
    )

    filled = rb.run_runtime_backfill(db_session, provider)
    assert filled == 3
    assert sorted(t.runtime_minutes for t in titles) == [100, 101, 102]  # NULLs filled
    assert all(t.vote_average == 6.5 for t in titles)  # quality signal healed alongside
    assert graded.runtime_minutes == 90  # never clobbered; also never fetched
    assert (graded.tmdb_id, TitleKind.movie) not in provider.calls

    # Second pass finds nothing NULL and no-ops (doesn't re-fetch the now-filled rows).
    provider.calls.clear()
    assert rb.run_runtime_backfill(db_session, provider) == 0
    assert provider.calls == []


def test_run_backfill_stops_on_a_title_tmdb_cannot_fill(db_session: Session) -> None:
    # A title TMDB returns no runtime for stays NULL; the loop must not re-select it forever.
    _add(db_session, tmdb_id=4000, runtime=None)
    db_session.flush()
    provider = FakeMetadataProvider(
        titles={(4000, TitleKind.movie): TitleMetadata(kind=TitleKind.movie, title="T4000")}
    )
    assert (
        rb.run_runtime_backfill(db_session, provider) == 0
    )  # nothing fillable → 0, and it returns
    assert len(provider.calls) == 1  # tried once, then stopped (didn't spin to the safety bound)


def test_run_backfill_walks_past_a_full_batch_of_unfillable_titles(db_session: Session) -> None:
    # Live R7 regression: the first batch was entirely TV-style titles TMDB returns no runtime for,
    # and the heal gave up at 609/7400. The keyset cursor must pass over them and keep walking.
    unfillable = [
        _add(db_session, tmdb_id=4000 + i, runtime=None) for i in range(rb._RUNTIME_BATCH)
    ]
    fillable = [_add(db_session, tmdb_id=9000 + i, runtime=None) for i in range(2)]
    db_session.flush()
    provider = FakeMetadataProvider(
        titles={
            **{
                (t.tmdb_id, TitleKind.movie): TitleMetadata(kind=TitleKind.movie, title=t.title)
                for t in unfillable
            },
            **{
                (t.tmdb_id, TitleKind.movie): TitleMetadata(
                    kind=TitleKind.movie, title=t.title, runtime_minutes=95
                )
                for t in fillable
            },
        }
    )
    assert rb.run_runtime_backfill(db_session, provider) == 2
    assert all(t.runtime_minutes == 95 for t in fillable)
    assert all(t.runtime_minutes is None for t in unfillable)  # skipped, not clobbered


def test_schedule_is_a_noop_without_a_tmdb_key(db_session: Session) -> None:
    # Offline: no provider to fetch runtimes from, so scheduling must no-op (never marks running).
    started = schedule_runtime_backfill(Settings(tmdb_api_key=None))
    assert started is False
    assert runtime_backfill_running() is False


def test_schedule_runs_via_injected_runner_when_key_present(db_session: Session) -> None:
    # With a key + a synchronous runner (no real thread), the scheduler fires the work exactly once.
    ran: list[bool] = []
    started = schedule_runtime_backfill(
        Settings(tmdb_api_key="x"), runner=lambda work: ran.append(True)
    )
    assert started is True
    assert ran == [True]

    # A second call while the first is "running" is deduped by the in-process lock.
    assert schedule_runtime_backfill(Settings(tmdb_api_key="x"), runner=lambda w: None) is False


def test_boot_gate_schedules_only_when_gap_is_large(db_session: Session) -> None:
    # The boot helper gates on the gap: a healthy catalog schedules nothing; a gappy one schedules.
    _add(db_session, tmdb_id=5000, runtime=100)  # fully covered → no heal
    db_session.flush()
    calls: list[bool] = []

    def fake_schedule(settings: Settings, **_: object) -> bool:
        calls.append(True)
        return True

    import phare.catalog.runtime_backfill as mod

    original = mod.schedule_runtime_backfill
    mod.schedule_runtime_backfill = fake_schedule  # type: ignore[assignment]
    try:
        assert _schedule_runtime_heal_if_gapped(db_session, Settings(tmdb_api_key="x")) is False
        assert calls == []  # gap 0 → nothing scheduled

        for i in range(4):  # now 4 of 5 titles are runtime-less → gap 0.8
            _add(db_session, tmdb_id=5100 + i, runtime=None)
        db_session.flush()
        assert _schedule_runtime_heal_if_gapped(db_session, Settings(tmdb_api_key="x")) is True
        assert calls == [True]
    finally:
        mod.schedule_runtime_backfill = original  # type: ignore[assignment]
