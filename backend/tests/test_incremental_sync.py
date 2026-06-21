"""Incremental sync: the per-source high-water mark and the `since` it feeds to providers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from phare.core.config import get_settings
from phare.core.sync_state import get_last_synced, set_last_synced
from phare.core.tokens import store_source_token
from phare.db.models import Profile
from phare.providers.trakt import TraktSourceProvider
from tests.conftest import authed_client, make_account


def test_sync_state_roundtrip_and_upsert(db_session: Session) -> None:
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()

    assert get_last_synced(db_session, profile.id, "trakt") is None

    first = datetime(2026, 1, 1, tzinfo=UTC)
    set_last_synced(db_session, profile.id, "trakt", first)
    assert get_last_synced(db_session, profile.id, "trakt") == first

    second = datetime(2026, 2, 1, tzinfo=UTC)
    set_last_synced(db_session, profile.id, "trakt", second)  # upsert, not duplicate
    assert get_last_synced(db_session, profile.id, "trakt") == second


def test_sync_is_incremental_then_full_override(db_session: Session, monkeypatch) -> None:
    monkeypatch.setenv("TRAKT_CLIENT_ID", "c")
    monkeypatch.setenv("TMDB_API_KEY", "t")
    monkeypatch.setenv("SECRET_KEY", "sign-me")
    get_settings.cache_clear()
    settings = get_settings()
    try:
        user = make_account(db_session)
        profile = user.profile
        store_source_token(db_session, settings, profile.id, "trakt", "acc")

        seen_since: list[datetime | None] = []

        def fake_pull(self: TraktSourceProvider, since=None):  # noqa: ANN001, ANN202
            seen_since.append(since)
            return iter([])

        monkeypatch.setattr(TraktSourceProvider, "pull", fake_pull)

        client = authed_client(db_session, user)
        body = {"profileId": str(profile.id)}

        assert client.post("/sources/trakt/sync", json=body).status_code == 200
        assert client.post("/sources/trakt/sync", json=body).status_code == 200
        assert client.post("/sources/trakt/sync", json={**body, "full": True}).status_code == 200

        assert seen_since[0] is None  # first sync: full history
        assert seen_since[1] is not None  # second sync: only since the watermark
        assert seen_since[2] is None  # full=True overrides the watermark
        assert get_last_synced(db_session, profile.id, "trakt") is not None
    finally:
        get_settings.cache_clear()
