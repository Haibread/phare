"""Background auto-sync: the recurring incremental sync of connected Trakt accounts (round 17).

Hermetic — no real Trakt/TMDB. The orchestration is driven synchronously with a fake per-profile
worker; the one real-path test fakes ``TraktSourceProvider.pull`` and asserts the watermark moves.
"""

from __future__ import annotations

import threading
import uuid
from unittest import mock

import pytest
from sqlalchemy.orm import Session

from phare.api.sync import (
    _auto_sync_configured,
    _profiles_with_trakt_token,
    run_auto_sync,
    start_auto_sync_loop,
)
from phare.core.config import get_settings
from phare.core.sync_state import get_last_synced
from phare.core.tokens import store_source_token
from phare.providers.trakt import TraktSourceProvider
from tests.conftest import make_account


@pytest.fixture
def trakt_settings(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201 - Settings type is internal
    """Settings with Trakt + TMDB configured (what unattended sync needs)."""
    monkeypatch.setenv("TRAKT_CLIENT_ID", "client")
    monkeypatch.setenv("TMDB_API_KEY", "tmdb")
    monkeypatch.setenv("SECRET_KEY", "sign-me")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _account_with_trakt(session: Session, settings) -> uuid.UUID:  # noqa: ANN001
    user = make_account(session)
    store_source_token(session, settings, user.profile.id, "trakt", "token")
    return user.profile.id


def test_auto_sync_syncs_each_profile_with_a_trakt_token(
    db_session: Session, trakt_settings
) -> None:  # noqa: ANN001
    # Every profile with a stored Trakt token is synced; a profile without one is left alone.
    with_token = _account_with_trakt(db_session, trakt_settings)
    without_token = make_account(db_session).profile.id
    db_session.flush()

    seen: list[uuid.UUID] = []

    def fake_worker(_session, _settings, profile_id, _language) -> None:  # noqa: ANN001
        seen.append(profile_id)

    synced = run_auto_sync(db_session, trakt_settings, sync_profile=fake_worker)

    assert synced == 1
    assert seen == [with_token]  # the token-less profile is never touched
    assert without_token not in seen


def test_auto_sync_is_best_effort_per_profile(db_session: Session, trakt_settings) -> None:  # noqa: ANN001
    # One profile's failure must not stop the others: it's recorded on the fallback metric and the
    # loop continues. Both are attempted; only the healthy one counts as synced.
    first = _account_with_trakt(db_session, trakt_settings)
    second = _account_with_trakt(db_session, trakt_settings)
    db_session.flush()

    attempted: list[uuid.UUID] = []

    def flaky_worker(_session, _settings, profile_id, _language) -> None:  # noqa: ANN001
        attempted.append(profile_id)
        if profile_id == first:
            raise RuntimeError("provider outage")

    with mock.patch("phare.api.sync.record_fallback") as record:
        synced = run_auto_sync(db_session, trakt_settings, sync_profile=flaky_worker)

    assert set(attempted) == {first, second}  # both attempted despite the first failing
    assert synced == 1  # only the healthy profile
    assert record.call_args_list == [mock.call("auto_sync", "profile_failed")]


def test_auto_sync_advances_the_watermark_through_the_real_worker(
    db_session: Session, trakt_settings, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    # End-to-end through the default worker (no fake): a pass with an empty Trakt pull still stamps
    # the incremental watermark, so the next pass asks only for events since then.
    profile_id = _account_with_trakt(db_session, trakt_settings)
    db_session.flush()

    seen_since: list[object] = []

    def fake_pull(_self, since=None):  # noqa: ANN001, ANN202
        seen_since.append(since)
        return iter([])

    monkeypatch.setattr(TraktSourceProvider, "pull", fake_pull)

    assert get_last_synced(db_session, profile_id, "trakt") is None
    run_auto_sync(db_session, trakt_settings)
    assert get_last_synced(db_session, profile_id, "trakt") is not None  # watermark stamped
    assert seen_since == [None]  # first pass: full history (no prior watermark)

    run_auto_sync(db_session, trakt_settings)
    assert seen_since[1] is not None  # second pass: only since the watermark (incremental)


def test_profiles_with_trakt_token_ignores_other_sources(
    db_session: Session, trakt_settings
) -> None:  # noqa: ANN001
    # Only Trakt tokens are auto-sync candidates — a Seerr (requests-only) token isn't a sync one.
    trakt_profile = _account_with_trakt(db_session, trakt_settings)
    seerr_user = make_account(db_session)
    store_source_token(db_session, trakt_settings, seerr_user.profile.id, "seerr", "key")
    db_session.flush()

    assert _profiles_with_trakt_token(db_session) == [trakt_profile]


def test_auto_sync_configured_requires_trakt_and_tmdb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAKT_CLIENT_ID", "client")
    monkeypatch.setenv("TMDB_API_KEY", "")
    get_settings.cache_clear()
    assert _auto_sync_configured(get_settings()) is False  # no TMDB → can't resolve titles
    monkeypatch.setenv("TMDB_API_KEY", "tmdb")
    get_settings.cache_clear()
    assert _auto_sync_configured(get_settings()) is True
    get_settings.cache_clear()


def test_start_auto_sync_loop_is_a_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Interval 0 (or unconfigured Trakt) → no thread spawned, and the returned stop() is safe.
    monkeypatch.setenv("TRAKT_CLIENT_ID", "client")
    monkeypatch.setenv("TMDB_API_KEY", "tmdb")
    monkeypatch.setenv("SOURCE_SYNC_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        stop = start_auto_sync_loop(get_settings())
        stop()  # must not raise
        assert not any(t.name == "source-auto-sync" for t in threading.enumerate())
    finally:
        get_settings.cache_clear()


def test_start_auto_sync_loop_starts_and_stops_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Configured + enabled: a daemon thread starts, waits out the initial delay, and stops on signal
    # without ever running a pass (a long initial delay keeps it parked so no real session opens).
    monkeypatch.setenv("TRAKT_CLIENT_ID", "client")
    monkeypatch.setenv("TMDB_API_KEY", "tmdb")
    monkeypatch.setenv("SOURCE_SYNC_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("SOURCE_SYNC_INITIAL_DELAY_SECONDS", "3600")
    get_settings.cache_clear()
    try:
        stop = start_auto_sync_loop(get_settings())
        assert any(t.name == "source-auto-sync" for t in threading.enumerate())
        stop()  # signals the event; the parked wait returns immediately
        assert not any(t.name == "source-auto-sync" and t.is_alive() for t in threading.enumerate())
    finally:
        get_settings.cache_clear()
