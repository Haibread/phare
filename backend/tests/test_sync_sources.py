"""Endpoint wiring + guards for the Plex/Jellyfin sync routes (no network)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.core.config import get_settings
from phare.core.tokens import store_source_token
from phare.db.models import EventType, TitleKind, WatchEvent
from phare.providers.fakes import FakeMetadataProvider
from phare.providers.jellyfin import list_jellyfin_users
from phare.providers.trakt import TraktSourceProvider
from phare.providers.types import RawEvent, RawMediaType, TitleMetadata
from tests.conftest import authed_client, make_account


def _resolvable_events(n: int) -> list[RawEvent]:
    return [
        RawEvent(
            source="trakt",
            media_type=RawMediaType.movie,
            type=EventType.watched,
            tmdb_id=1000 + i,
            external_ref=f"trakt:{i}",
        )
        for i in range(n)
    ]


def _fake_metadata(n: int) -> FakeMetadataProvider:
    return FakeMetadataProvider(
        titles={
            (1000 + i, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie, tmdb_id=1000 + i, title=f"Movie {1000 + i}", year=2020
            )
            for i in range(n)
        }
    )


def test_plex_sync_requires_tmdb(db_session: Session) -> None:
    user = make_account(db_session)
    response = authed_client(db_session, user).post(
        "/sources/plex/sync",
        json={"profileId": str(user.profile.id), "baseUrl": "http://plex", "token": "t"},
    )
    assert response.status_code == 400
    assert "TMDB" in response.json()["detail"]


def test_jellyfin_sync_requires_tmdb(db_session: Session) -> None:
    user = make_account(db_session)
    response = authed_client(db_session, user).post(
        "/sources/jellyfin/sync",
        json={
            "profileId": str(user.profile.id),
            "baseUrl": "http://jf",
            "userId": "u1",
            "apiKey": "k",
        },
    )
    assert response.status_code == 400


def test_plex_sync_rejects_ssrf_base_url(db_session: Session) -> None:
    # The metadata IP must be rejected before any server-side fetch (and before the TMDB check).
    user = make_account(db_session)
    response = authed_client(db_session, user).post(
        "/sources/plex/sync",
        json={
            "profileId": str(user.profile.id),
            "baseUrl": "http://169.254.169.254/",
            "token": "t",
        },
    )
    assert response.status_code == 400
    assert "blocked" in response.json()["detail"]


def test_capabilities_reflect_server_config(db_session: Session, monkeypatch) -> None:
    # No TMDB key → every history source is unavailable; Seerr is always offered (review D1).
    user = make_account(db_session)
    client = authed_client(db_session, user)
    body = client.get("/sources/capabilities").json()
    assert body == {
        "trakt": False,
        "plex": False,
        "jellyfin": False,
        "seerr": True,
        "sampleData": True,
    }

    # TMDB set enables Plex/Jellyfin; Trakt still needs its OAuth app credentials.
    monkeypatch.setenv("TMDB_API_KEY", "t")
    get_settings.cache_clear()
    body = client.get("/sources/capabilities").json()
    assert body == {
        "trakt": False,
        "plex": True,
        "jellyfin": True,
        "seerr": True,
        "sampleData": True,
    }

    monkeypatch.setenv("TRAKT_CLIENT_ID", "c")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "s")
    get_settings.cache_clear()
    assert client.get("/sources/capabilities").json()["trakt"] is True


def test_capabilities_hide_sample_data_in_production(db_session: Session, monkeypatch) -> None:
    # The sample endpoints 403 in production; capabilities must report sampleData=false so the UI
    # hides the escape hatch instead of offering a button that hangs on the rejection.
    user = make_account(db_session)
    client = authed_client(db_session, user)
    assert client.get("/sources/capabilities").json()["sampleData"] is True

    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    assert client.get("/sources/capabilities").json()["sampleData"] is False


def test_sync_partial_failure_reports_ingested_and_resumes(
    db_session: Session, monkeypatch
) -> None:
    # A sync that dies after the first committed batch answers 502 with a structured body carrying
    # the ingested count; the committed data stays, and a re-run resumes without dupes (review G3).
    monkeypatch.setenv("TRAKT_CLIENT_ID", "c")
    monkeypatch.setenv("TMDB_API_KEY", "t")
    monkeypatch.setenv("SECRET_KEY", "sign-me")
    get_settings.cache_clear()
    settings = get_settings()
    try:
        user = make_account(db_session)
        profile = user.profile
        store_source_token(db_session, settings, profile.id, "trakt", "acc")
        # Swap the real TMDB resolver for an in-memory one so the ingest needs no network.
        monkeypatch.setattr("phare.api.sync.TMDBMetadataProvider", lambda **_: _fake_metadata(200))

        def exploding_pull(self: TraktSourceProvider, since=None) -> Iterator[RawEvent]:  # noqa: ANN001
            def gen() -> Iterator[RawEvent]:
                yield from _resolvable_events(100)  # one full default batch commits...
                raise RuntimeError("source blew up mid-sync")  # ...then the source dies

            return gen()

        monkeypatch.setattr(TraktSourceProvider, "pull", exploding_pull)

        client = authed_client(db_session, user)
        body = {"profileId": str(profile.id)}
        response = client.post("/sources/trakt/sync", json=body)
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert detail["code"] == "sync_partial_failure"
        assert detail["ingested"] >= 100

        def count_events() -> int:
            return db_session.scalar(
                select(func.count())
                .select_from(WatchEvent)
                .where(WatchEvent.profile_id == profile.id)
            )

        assert count_events() == 100  # first batch is durable, not rolled back

        # Re-run: the same 100 + 20 more, no failure. Idempotent upsert → no duplicates.
        monkeypatch.setattr(
            TraktSourceProvider, "pull", lambda self, since=None: iter(_resolvable_events(120))
        )
        assert client.post("/sources/trakt/sync", json=body).status_code == 200
        assert count_events() == 120
    finally:
        get_settings.cache_clear()


def test_jellyfin_users_lists_names_and_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Users"
        return httpx.Response(
            200,
            json=[
                {"Id": "abc", "Name": "Alice"},
                {"Id": "def", "Name": "Bob"},
                {"Name": "no-id-skipped"},
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://jf.test")
    users = list_jellyfin_users("http://jf.test", "key", client=client)
    assert users == [{"id": "abc", "name": "Alice"}, {"id": "def", "name": "Bob"}]


def test_jellyfin_users_endpoint_rejects_ssrf(db_session: Session) -> None:
    user = make_account(db_session)
    response = authed_client(db_session, user).post(
        "/sources/jellyfin/users",
        json={"baseUrl": "http://169.254.169.254/", "apiKey": "k"},
    )
    assert response.status_code == 400
    assert "blocked" in response.json()["detail"]


def test_jellyfin_users_endpoint_returns_picker_list(db_session: Session, monkeypatch) -> None:
    user = make_account(db_session)

    def fake_list(base_url: str, api_key: str) -> list[dict[str, str]]:
        assert (base_url, api_key) == ("http://jf.test", "k")
        return [{"id": "u1", "name": "Alice"}]

    monkeypatch.setattr("phare.api.sync.list_jellyfin_users", fake_list)
    response = authed_client(db_session, user).post(
        "/sources/jellyfin/users",
        json={"baseUrl": "http://jf.test", "apiKey": "k"},
    )
    assert response.status_code == 200
    assert response.json() == [{"id": "u1", "name": "Alice"}]
