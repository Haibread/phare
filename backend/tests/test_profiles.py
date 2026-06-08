"""Profiles API + sample-data seeding + interim sync guard."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.db.base import get_session


def _client(session: Session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_create_and_list_profiles(db_session: Session) -> None:
    client = _client(db_session)

    created = client.post("/profiles", json={"displayName": "Theo"})
    assert created.status_code == 201
    assert created.json()["displayName"] == "Theo"

    listed = client.get("/profiles")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["displayName"] == "Theo"


def test_sample_data_then_history(db_session: Session) -> None:
    client = _client(db_session)
    profile_id = client.post("/profiles", json={"displayName": "Theo"}).json()["id"]

    seeded = client.post(f"/profiles/{profile_id}/sample-data")
    assert seeded.status_code == 200
    assert seeded.json()["created"] == 6
    assert seeded.json()["titlesCreated"] == 3

    history = client.get("/history", params={"profileId": profile_id}).json()
    assert history["total"] == 6
    titles = {item["title"] for item in history["items"]}
    assert "Dune" in titles
    assert "Severance" in titles


def test_sample_data_unknown_profile_404(db_session: Session) -> None:
    client = _client(db_session)
    response = client.post(f"/profiles/{uuid.uuid4()}/sample-data")
    assert response.status_code == 404


def test_sync_without_credentials_400(db_session: Session) -> None:
    client = _client(db_session)
    profile_id = client.post("/profiles", json={"displayName": "Theo"}).json()["id"]
    response = client.post(
        "/sources/trakt/sync",
        json={"profileId": profile_id, "accessToken": "token"},
    )
    assert response.status_code == 400
