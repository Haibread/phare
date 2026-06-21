"""Memory foundation: commitment + note stores and the inspect/edit endpoints (no LLM)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from phare.agent import commitments as commitments_store
from phare.agent import memory as memory_store
from phare.db.models import CommitmentStatus, MemoryKind, Title, TitleKind, User
from tests.conftest import authed_client, make_account

_NOW = datetime(2026, 6, 20, tzinfo=UTC)


def _account_and_title(session: Session) -> tuple[User, uuid.UUID]:
    user = make_account(session)
    title = Title(kind=TitleKind.movie, tmdb_id=603, title="The Matrix")
    session.add(title)
    session.flush()
    return user, title.id


# --- commitment store --------------------------------------------------------


def test_commitment_lifecycle(db_session: Session) -> None:
    user, title_id = _account_and_title(db_session)
    profile_id = user.profile.id
    c = commitments_store.create_commitment(db_session, profile_id, title_id, note="tonight")

    assert c.status is CommitmentStatus.pending
    assert [x.id for x in commitments_store.pending_commitments(db_session, profile_id)] == [c.id]

    commitments_store.resolve_commitment(c, status=CommitmentStatus.watched, resolved_at=_NOW)
    assert commitments_store.pending_commitments(db_session, profile_id) == []
    assert c.resolved_at == _NOW


# --- memory note store -------------------------------------------------------


def test_active_notes_filters_expired(db_session: Session) -> None:
    profile_id = make_account(db_session).profile.id
    durable = memory_store.create_note(
        db_session, profile_id, "no gore", kind=MemoryKind.preference
    )
    fresh = memory_store.create_note(
        db_session,
        profile_id,
        "kid-friendly",
        kind=MemoryKind.context,
        expires_at=_NOW + timedelta(days=10),
    )
    memory_store.create_note(
        db_session,
        profile_id,
        "halloween mood",
        kind=MemoryKind.context,
        expires_at=_NOW - timedelta(days=1),
    )

    active = {n.id for n in memory_store.active_notes(db_session, profile_id, now=_NOW)}
    assert active == {durable.id, fresh.id}  # the expired note is dropped
    assert len(memory_store.list_notes(db_session, profile_id)) == 3  # list shows everything


# --- endpoints ---------------------------------------------------------------


def test_memory_endpoints_add_list_delete(db_session: Session) -> None:
    user = make_account(db_session)
    profile_id = user.profile.id
    client = authed_client(db_session, user)

    created = client.post(
        f"/profiles/{profile_id}/memory",
        json={"text": "loves slow-burn sci-fi", "kind": "preference"},
    )
    assert created.status_code == 200
    note_id = created.json()["id"]

    listed = client.get(f"/profiles/{profile_id}/memory").json()["items"]
    assert [n["text"] for n in listed] == ["loves slow-burn sci-fi"]
    assert listed[0]["source"] == "manual"

    assert client.delete(f"/profiles/{profile_id}/memory/{note_id}").status_code == 204
    assert client.get(f"/profiles/{profile_id}/memory").json()["items"] == []


def test_memory_add_rejects_unknown_kind(db_session: Session) -> None:
    user = make_account(db_session)
    resp = authed_client(db_session, user).post(
        f"/profiles/{user.profile.id}/memory", json={"text": "x", "kind": "bogus"}
    )
    assert resp.status_code == 422


def test_commitments_endpoint_lists_with_title(db_session: Session) -> None:
    user, title_id = _account_and_title(db_session)
    profile_id = user.profile.id
    commitments_store.create_commitment(db_session, profile_id, title_id)
    db_session.flush()

    items = (
        authed_client(db_session, user).get(f"/profiles/{profile_id}/commitments").json()["items"]
    )
    assert len(items) == 1
    assert items[0]["title"] == "The Matrix"
    assert items[0]["status"] == "pending"


def test_chat_opening_follows_up_on_pending_plans(db_session: Session) -> None:
    user, title_id = _account_and_title(db_session)
    profile_id = user.profile.id
    client = authed_client(db_session, user)

    assert client.get(f"/profiles/{profile_id}/chat/opening").json()["greeting"] is None

    commitments_store.create_commitment(db_session, profile_id, title_id)
    db_session.flush()
    greeting = client.get(f"/profiles/{profile_id}/chat/opening").json()["greeting"]
    assert greeting is not None and "The Matrix" in greeting


def test_chat_undo_removes_a_chat_event(db_session: Session) -> None:
    from sqlalchemy import select

    from phare.db.models import EventType, WatchEvent

    user, title_id = _account_and_title(db_session)
    profile_id = user.profile.id
    event = WatchEvent(
        profile_id=profile_id,
        title_id=title_id,
        type=EventType.watched,
        source="chat",
        external_ref="chat:x",
    )
    db_session.add(event)
    db_session.flush()

    resp = authed_client(db_session, user).post(
        f"/profiles/{profile_id}/chat/undo", json={"token": f"event:{event.id}"}
    )
    assert resp.status_code == 200 and resp.json()["undone"] is True
    assert (
        db_session.scalars(select(WatchEvent).where(WatchEvent.profile_id == profile_id)).all()
        == []
    )


def test_memory_delete_other_profile_is_404(db_session: Session) -> None:
    user = make_account(db_session)
    profile_id = user.profile.id
    other = make_account(db_session, display_name="other", email="other@example.test")
    note = memory_store.create_note(db_session, profile_id, "mine")
    db_session.flush()

    # Deleting profile A's note via another user's profile path must not succeed (isolation):
    # another user's profile is invisible — a 404, not a leak.
    assert (
        authed_client(db_session, user)
        .delete(f"/profiles/{other.profile.id}/memory/{note.id}")
        .status_code
        == 404
    )
