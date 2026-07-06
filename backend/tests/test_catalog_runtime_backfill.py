"""Bulk background runtime heal (round-7 finding 2).

The broad discover import omits runtime, so a runtime cap filters on a ghost catalog. At boot, when
most titles are runtime-less, a background thread walks the NULL-runtime rows and fetches each
title's TMDB detail. These tests drive the "background" work synchronously (the real scheduler
spawns a daemon thread with its own DB connection a test can't roll back), and never hit a network.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import phare.catalog.runtime_backfill as rb
from phare.catalog.bootstrap import _schedule_runtime_heal_if_gapped
from phare.catalog.heal import RateLimiter
from phare.catalog.runtime_backfill import (
    metadata_gap,
    runtime_backfill_running,
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


def _add(
    session: Session,
    *,
    tmdb_id: int,
    runtime: int | None,
    language: str | None = "en",
    **extra: object,
) -> Title:
    # Default a language so a row with a runtime is fully covered (no gap) — the gap/backfill tests
    # opt a row into "gapped" by passing ``runtime=None`` and/or ``language=None`` explicitly.
    title = Title(
        kind=TitleKind.movie,
        tmdb_id=tmdb_id,
        title=f"T{tmdb_id}",
        runtime_minutes=runtime,
        original_language=language,
        **extra,
    )
    session.add(title)
    return title


def test_metadata_gap_is_share_of_gapped_titles(db_session: Session) -> None:
    assert metadata_gap(db_session) == 0.0  # empty catalog
    for i in range(3):
        _add(db_session, tmdb_id=1000 + i, runtime=None)  # missing runtime → gapped
    _add(db_session, tmdb_id=2000, runtime=100)  # runtime + default language → covered
    db_session.flush()
    assert metadata_gap(db_session) == 0.75  # 3 of 4 missing


def test_metadata_gap_counts_runtimed_but_languageless_rows(db_session: Session) -> None:
    # A catalog fully runtimed but with no original_language must still read a large gap so one
    # background pass fires (round-8: credits/language heal, not just runtime).
    for i in range(4):
        _add(db_session, tmdb_id=6000 + i, runtime=100, language=None)  # runtimed, no language
    db_session.flush()
    assert metadata_gap(db_session) == 1.0


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

    filled = rb.run_metadata_backfill(db_session, provider)
    assert filled == 3
    assert sorted(t.runtime_minutes for t in titles) == [100, 101, 102]  # NULLs filled
    assert all(t.vote_average == 6.5 for t in titles)  # quality signal healed alongside
    assert graded.runtime_minutes == 90  # never clobbered; also never fetched
    assert (graded.tmdb_id, TitleKind.movie) not in provider.calls

    # Second pass finds nothing NULL and no-ops (doesn't re-fetch the now-filled rows).
    provider.calls.clear()
    assert rb.run_metadata_backfill(db_session, provider) == 0
    assert provider.calls == []


def test_run_backfill_stops_on_a_title_tmdb_cannot_fill(db_session: Session) -> None:
    # A title TMDB returns no runtime for stays NULL; the loop must not re-select it forever.
    _add(db_session, tmdb_id=4000, runtime=None)
    db_session.flush()
    provider = FakeMetadataProvider(
        titles={(4000, TitleKind.movie): TitleMetadata(kind=TitleKind.movie, title="T4000")}
    )
    assert (
        rb.run_metadata_backfill(db_session, provider) == 0
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
    assert rb.run_metadata_backfill(db_session, provider) == 2
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


def test_run_backfill_fills_credits_and_language_without_clobbering(db_session: Session) -> None:
    # Round-8: a row missing language (but already runtimed) is revisited, and its credits/language
    # are healed. A row that already has directors/language keeps them — the heal never clobbers.
    gapped = _add(db_session, tmdb_id=7000, runtime=110, language=None)  # runtimed, no language
    kept = _add(
        db_session,
        tmdb_id=7100,
        runtime=120,
        language=None,  # gapped by language so it's selected...
        directors=["Existing Director"],  # ...but its directors must not be clobbered
    )
    db_session.flush()
    provider = FakeMetadataProvider(
        titles={
            (7000, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie,
                title="T7000",
                directors=["Denis Villeneuve"],
                top_cast=["A", "B", "C"],
                original_language="en",
            ),
            (7100, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie,
                title="T7100",
                directors=["New Director"],  # would clobber if the heal weren't guarded
                original_language="fr",
            ),
        }
    )
    assert rb.run_metadata_backfill(db_session, provider) == 2

    assert gapped.directors == ["Denis Villeneuve"]
    assert gapped.top_cast == ["A", "B", "C"]
    assert gapped.original_language == "en"
    assert kept.directors == ["Existing Director"]  # non-empty credits never overwritten
    assert kept.original_language == "fr"  # language WAS NULL, so it fills


def test_run_backfill_revisits_runtime_filled_but_creditless_rows(db_session: Session) -> None:
    # The live gotcha: movies got their runtimes healed live, so a runtime-only predicate would skip
    # them all. A row with a runtime but no language must still be visited and get its credits.
    row = _add(db_session, tmdb_id=8000, runtime=95, language=None)
    db_session.flush()
    provider = FakeMetadataProvider(
        titles={
            (8000, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie,
                title="T8000",
                directors=["Someone"],
                original_language="ja",
            )
        }
    )
    assert rb.run_metadata_backfill(db_session, provider) == 1
    assert row.directors == ["Someone"]
    assert row.original_language == "ja"


def test_bulk_backfill_rate_limits_via_injected_limiter(db_session: Session) -> None:
    # The bulk walker throttles every fetch through the limiter. Drive it on a fake clock so the
    # cap is exercised with no real sleeping — three fetches at 30 rps space out by ~1/30 s each.
    for i in range(3):
        _add(db_session, tmdb_id=8500 + i, runtime=None)
    db_session.flush()
    provider = FakeMetadataProvider(
        titles={
            (8500 + i, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie, title=f"T{8500 + i}", runtime_minutes=100
            )
            for i in range(3)
        }
    )
    slept: list[float] = []
    clock = [0.0]

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds  # advance the fake clock as if we waited

    limiter = RateLimiter(30.0, clock=lambda: clock[0], sleep=fake_sleep)
    assert rb.run_metadata_backfill(db_session, provider, limiter=limiter) == 3
    # First token is free; the next two each wait one 1/30 s interval (the fan-out is bounded, so
    # acquisitions serialize through the limiter's lock — steady drip).
    assert slept and all(abs(s - 1.0 / 30.0) < 1e-9 for s in slept)


def test_heal_stack_recovers_from_a_single_429(db_session: Session) -> None:
    # A provider that 429s once then succeeds must still heal the row — the TMDB HTTP layer honours
    # Retry-After and retries inside get_title, so the walker sees a clean fetch.
    import httpx

    from phare.providers.tmdb import TMDBMetadataProvider

    _add(db_session, tmdb_id=8800, runtime=None)
    db_session.flush()

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            json={
                "id": 8800,
                "title": "T8800",
                "runtime": 100,
                "original_language": "en",
                "keywords": {"keywords": []},
                "credits": {"crew": [], "cast": []},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://tmdb.test")
    provider = TMDBMetadataProvider(api_key="x", client=client, cache_ttl=0, sleep=lambda _s: None)
    assert rb.run_metadata_backfill(db_session, provider) == 1
    row = db_session.scalar(select(Title).where(Title.tmdb_id == 8800))
    assert row is not None and row.runtime_minutes == 100
    assert calls["n"] == 2  # 429 then success
