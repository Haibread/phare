"""Memory foundation: commitment + note stores and the inspect/edit endpoints (no LLM)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.agent import commitments as commitments_store
from phare.agent import memory as memory_store
from phare.api.app import create_app
from phare.db.base import get_session
from phare.db.models import CommitmentStatus, MemoryKind, Profile, Title, TitleKind

_NOW = datetime(2026, 6, 20, tzinfo=UTC)


def _profile_and_title(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    profile = Profile(display_name="me")
    title = Title(kind=TitleKind.movie, tmdb_id=603, title="The Matrix")
    session.add_all([profile, title])
    session.flush()
    return profile.id, title.id


def _client(session: Session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


# --- commitment store --------------------------------------------------------


def test_commitment_lifecycle(db_session: Session) -> None:
    profile_id, title_id = _profile_and_title(db_session)
    c = commitments_store.create_commitment(db_session, profile_id, title_id, note="tonight")

    assert c.status is CommitmentStatus.pending
    assert [x.id for x in commitments_store.pending_commitments(db_session, profile_id)] == [c.id]

    commitments_store.resolve_commitment(c, status=CommitmentStatus.watched, resolved_at=_NOW)
    assert commitments_store.pending_commitments(db_session, profile_id) == []
    assert c.resolved_at == _NOW


# --- memory note store -------------------------------------------------------


def test_active_notes_filters_expired(db_session: Session) -> None:
    profile_id, _ = _profile_and_title(db_session)
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
    profile_id, _ = _profile_and_title(db_session)
    client = _client(db_session)

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
    profile_id, _ = _profile_and_title(db_session)
    resp = _client(db_session).post(
        f"/profiles/{profile_id}/memory", json={"text": "x", "kind": "bogus"}
    )
    assert resp.status_code == 422


def test_commitments_endpoint_lists_with_title(db_session: Session) -> None:
    profile_id, title_id = _profile_and_title(db_session)
    commitments_store.create_commitment(db_session, profile_id, title_id)
    db_session.flush()

    items = _client(db_session).get(f"/profiles/{profile_id}/commitments").json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "The Matrix"
    assert items[0]["status"] == "pending"


def test_memory_delete_other_profile_is_404(db_session: Session) -> None:
    profile_id, _ = _profile_and_title(db_session)
    other = Profile(display_name="other")
    db_session.add(other)
    db_session.flush()
    note = memory_store.create_note(db_session, profile_id, "mine")
    db_session.flush()

    # Deleting profile A's note via profile B's path must not succeed (per-profile isolation).
    assert _client(db_session).delete(f"/profiles/{other.id}/memory/{note.id}").status_code == 404
