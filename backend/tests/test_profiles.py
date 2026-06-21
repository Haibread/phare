"""Profiles API + sample-data seeding + interim sync guard."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from tests.conftest import authed_client, make_account


def test_account_profile_is_listed(db_session: Session) -> None:
    user = make_account(db_session, display_name="Theo")
    client = authed_client(db_session, user)

    listed = client.get("/profiles")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["displayName"] == "Theo"
    assert body["items"][0]["id"] == str(user.profile.id)


def test_sample_data_then_history(db_session: Session) -> None:
    user = make_account(db_session)
    profile_id = user.profile.id
    client = authed_client(db_session, user)

    seeded = client.post(f"/profiles/{profile_id}/sample-data")
    assert seeded.status_code == 200
    assert seeded.json()["created"] == 6
    assert seeded.json()["titlesCreated"] == 3

    history = client.get("/history", params={"profileId": str(profile_id)}).json()
    assert history["total"] == 6
    titles = {item["title"] for item in history["items"]}
    assert "Dune" in titles
    assert "Severance" in titles


def test_sample_data_unknown_profile_404(db_session: Session) -> None:
    user = make_account(db_session)
    response = authed_client(db_session, user).post(f"/profiles/{uuid.uuid4()}/sample-data")
    assert response.status_code == 404


def test_sync_without_credentials_400(db_session: Session) -> None:
    user = make_account(db_session)
    profile_id = user.profile.id
    response = authed_client(db_session, user).post(
        "/sources/trakt/sync",
        json={"profileId": str(profile_id), "accessToken": "token"},
    )
    assert response.status_code == 400
