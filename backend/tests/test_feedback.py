"""Card feedback: a "not interested" signal is recorded, drops the title from recs, and undoes."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.deps import Embedder, get_embedder, get_optional_chat_llm
from phare.db.models import EventType, User, WatchEvent
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from tests.conftest import authed_client, make_account


def _client(session: Session, user: User) -> TestClient:
    overrides = {
        get_embedder: lambda: Embedder(
            provider=LocalHashEmbeddingProvider(),
            read_version=LOCAL_MODEL_VERSION,
            write_version=LOCAL_MODEL_VERSION,
        ),
        get_optional_chat_llm: lambda: None,
    }
    return authed_client(session, user, overrides=overrides)


def _seed(client: TestClient, user: User) -> str:
    profile_id = str(user.profile.id)
    client.post(f"/profiles/{profile_id}/sample-data")
    client.post("/catalog/sample")
    return profile_id


def _recommended_ids(client: TestClient, profile_id: str) -> set[str]:
    rows = client.get(f"/profiles/{profile_id}/recommendations").json()["rows"]
    return {item["titleId"] for row in rows for item in row["items"]}


def test_not_interested_drops_the_title_and_undo_restores_it(db_session: Session) -> None:
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = _seed(client, user)

    # "Not interested" is offered on *new* picks, so target an unwatched recommendation (rows like
    # watch-again carry already-watched titles, which would already own a watch event). Sorted for a
    # deterministic pick — set iteration order would otherwise flake the single-event assertions.
    shown = _recommended_ids(client, profile_id)
    watched = {
        str(tid)
        for tid in db_session.scalars(
            select(WatchEvent.title_id).where(WatchEvent.profile_id == uuid.UUID(profile_id))
        )
    }
    fresh = sorted(shown - watched)
    assert fresh, "expected an unwatched pick to reject"
    target = fresh[0]

    resp = client.post(
        f"/profiles/{profile_id}/titles/{target}/feedback", json={"signal": "not_interested"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["titleId"] == target
    assert body["signal"] == "not_interested"
    assert body["undoToken"].startswith("event:")

    # The signal is persisted as a negative event on the title...
    event = db_session.scalar(
        select(WatchEvent).where(
            WatchEvent.profile_id == uuid.UUID(profile_id),
            WatchEvent.title_id == uuid.UUID(target),
        )
    )
    assert event is not None and event.type is EventType.not_interested
    # ...and the title no longer surfaces in recommendations.
    assert target not in _recommended_ids(client, profile_id)

    # Undo reuses the chat mechanism: the token reverses the write and the title can return.
    undo = client.post(f"/profiles/{profile_id}/chat/undo", json={"token": body["undoToken"]})
    assert undo.status_code == 200 and undo.json()["undone"] is True
    assert (
        db_session.scalar(select(WatchEvent).where(WatchEvent.title_id == uuid.UUID(target)))
        is None
    )
    assert target in _recommended_ids(client, profile_id)


def test_loved_seeds_watched_and_liked_and_unblocks_onboarding(db_session: Session) -> None:
    """The onboarding "start from scratch" path (round 7, fix 1): loving a catalog title with no
    connected history writes a watched+liked pair and flips the onboarding gate to ready-to-browse.
    """
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = str(user.profile.id)
    # Only a catalog — no sample history. This mirrors a fresh account seeding by hand.
    client.post("/catalog/sample")

    # Before any loved pick, history isn't ready and Browse is gated.
    before = client.get(f"/profiles/{profile_id}/onboarding").json()
    assert before["historyReady"] is False
    assert before["readyToBrowse"] is False

    target = next(iter(_recommended_ids(client, profile_id)))
    resp = client.post(f"/profiles/{profile_id}/titles/{target}/feedback", json={"signal": "loved"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["signal"] == "loved"
    # A loved pick is two events, so the undo token is a comma-joined pair.
    tokens = body["undoToken"].split(",")
    assert len(tokens) == 2
    assert all(token.startswith("event:") for token in tokens)

    # Both a watched and a liked event landed on the title.
    events = db_session.scalars(
        select(WatchEvent).where(
            WatchEvent.profile_id == uuid.UUID(profile_id),
            WatchEvent.title_id == uuid.UUID(target),
        )
    ).all()
    assert {e.type for e in events} == {EventType.watched, EventType.liked}

    # The seeded events unblock Browse: history (and, offline, taste) now read ready.
    after = client.get(f"/profiles/{profile_id}/onboarding").json()
    assert after["historyReady"] is True
    assert after["readyToBrowse"] is True

    # Undo reverses the whole pair (the chat undo mechanism handles the comma-joined token).
    undo = client.post(f"/profiles/{profile_id}/chat/undo", json={"token": body["undoToken"]})
    assert undo.status_code == 200 and undo.json()["undone"] is True
    assert (
        db_session.scalar(select(WatchEvent).where(WatchEvent.title_id == uuid.UUID(target)))
        is None
    )


def test_feedback_unknown_title_is_404(db_session: Session) -> None:
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = _seed(client, user)
    resp = client.post(
        f"/profiles/{profile_id}/titles/{uuid.uuid4()}/feedback",
        json={"signal": "not_interested"},
    )
    assert resp.status_code == 404


def test_feedback_unknown_signal_is_422(db_session: Session) -> None:
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = _seed(client, user)
    # Grab any real title so we get past the 404 and hit signal validation.
    target = next(iter(_recommended_ids(client, profile_id)))
    resp = client.post(
        f"/profiles/{profile_id}/titles/{target}/feedback", json={"signal": "love_it"}
    )
    assert resp.status_code == 422


def test_feedback_is_isolated_to_the_caller(db_session: Session) -> None:
    owner = make_account(db_session, display_name="owner", email="owner@example.test")
    other = make_account(db_session, display_name="other", email="other@example.test")
    owner_client = _client(db_session, owner)
    profile_id = _seed(owner_client, owner)
    target = next(iter(_recommended_ids(owner_client, profile_id)))

    # A different user posting against the owner's profile is a 404 (same isolation as elsewhere).
    other_client = _client(db_session, other)
    resp = other_client.post(
        f"/profiles/{profile_id}/titles/{target}/feedback", json={"signal": "not_interested"}
    )
    assert resp.status_code == 404
