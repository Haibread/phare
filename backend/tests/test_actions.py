"""Seerr action provider: availability parsing, HTTP, and the API endpoints."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.db.base import get_session
from phare.db.models import Profile, Title, TitleKind
from phare.providers.seerr import SeerrProvider, availability_from_media_info
from phare.providers.types import MediaAvailability


def _client(session: Session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _seerr_client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://seerr.test")


# --- parsing ----------------------------------------------------------------


def test_availability_parser_maps_statuses() -> None:
    assert availability_from_media_info(None) is MediaAvailability.requestable
    assert availability_from_media_info({"status": 5}) is MediaAvailability.available
    assert availability_from_media_info({"status": 4}) is MediaAvailability.available
    assert availability_from_media_info({"status": 3}) is MediaAvailability.queued
    assert availability_from_media_info({"status": 2}) is MediaAvailability.queued
    assert availability_from_media_info({"status": 1}) is MediaAvailability.requestable


# --- provider HTTP ----------------------------------------------------------


def test_provider_availability_reads_media_info() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/movie/603"
        return httpx.Response(200, json={"mediaInfo": {"status": 5}})

    provider = SeerrProvider("https://seerr.test", "k", client=_seerr_client(handler))
    assert provider.availability(603, TitleKind.movie) is MediaAvailability.available


def test_provider_availability_404_is_requestable() -> None:
    provider = SeerrProvider(
        "https://seerr.test", "k", client=_seerr_client(lambda _r: httpx.Response(404))
    )
    assert provider.availability(1, TitleKind.movie) is MediaAvailability.requestable


def test_provider_request_posts_media() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/request"
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(201, json={"id": 1})

    provider = SeerrProvider("https://seerr.test", "k", client=_seerr_client(handler))
    assert provider.request_title(603, TitleKind.movie) is True
    assert seen == {"mediaType": "movie", "mediaId": 603}


def test_provider_request_show_includes_seasons() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(201, json={"id": 1})

    provider = SeerrProvider("https://seerr.test", "k", client=_seerr_client(handler))
    provider.request_title(1399, TitleKind.show)
    assert seen["mediaType"] == "tv"
    assert seen["seasons"] == "all"


# --- endpoints --------------------------------------------------------------


class _FakeAction:
    name = "fake"

    def availability(self, tmdb_id: int, kind: TitleKind) -> MediaAvailability:
        return MediaAvailability.requestable

    def request_title(self, tmdb_id: int, kind: TitleKind) -> bool:
        return True


def _profile_and_title(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    profile = Profile(display_name="me")
    title = Title(kind=TitleKind.movie, tmdb_id=603, title="The Matrix")
    session.add_all([profile, title])
    session.flush()
    return profile.id, title.id


def test_availability_unconfigured_reports_off(db_session: Session) -> None:
    profile_id, title_id = _profile_and_title(db_session)
    body = (
        _client(db_session)
        .post(f"/profiles/{profile_id}/availability", json={"titleIds": [str(title_id)]})
        .json()
    )
    assert body == {"configured": False, "results": {}}


def test_availability_configured_returns_status(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_id, title_id = _profile_and_title(db_session)
    monkeypatch.setattr("phare.api.actions.seerr_provider", lambda s, p: _FakeAction())
    body = (
        _client(db_session)
        .post(f"/profiles/{profile_id}/availability", json={"titleIds": [str(title_id)]})
        .json()
    )
    assert body["configured"] is True
    assert body["results"][str(title_id)] == "requestable"


def test_request_unconfigured_400(db_session: Session) -> None:
    profile_id, title_id = _profile_and_title(db_session)
    resp = _client(db_session).post(
        f"/profiles/{profile_id}/requests", json={"titleId": str(title_id)}
    )
    assert resp.status_code == 400


def test_request_hands_off_and_queues(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    profile_id, title_id = _profile_and_title(db_session)
    monkeypatch.setattr("phare.api.actions.seerr_provider", lambda s, p: _FakeAction())
    resp = _client(db_session).post(
        f"/profiles/{profile_id}/requests", json={"titleId": str(title_id)}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "availability": "queued"}


def test_connect_seerr_rejects_ssrf_base_url(db_session: Session) -> None:
    profile_id, _ = _profile_and_title(db_session)
    resp = _client(db_session).post(
        f"/profiles/{profile_id}/sources/seerr/connect",
        json={"baseUrl": "http://127.0.0.1:5055", "apiKey": "k"},
    )
    assert resp.status_code == 400
    assert "blocked" in resp.json()["detail"]
