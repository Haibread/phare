"""Inspect + edit the chat agent's memory: watch commitments and generalist notes.

Memory is never a black box (principle 2): everything the agent remembers is listable here and,
for notes, hand-editable — the same posture as the taste profile.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from phare.agent import commitments as commitments_store
from phare.agent import memory as memory_store
from phare.api.recommend import _poster_url, require_profile
from phare.api.schemas import (
    CommitmentItem,
    CommitmentList,
    CreateMemoryNoteRequest,
    MemoryNoteItem,
    MemoryNoteList,
)
from phare.db.base import get_session
from phare.db.models import MemoryKind, MemoryNote, Title, WatchCommitment

router = APIRouter(tags=["Memory"])


def _commitment_item(session: Session, commitment: WatchCommitment) -> CommitmentItem:
    title = session.get(Title, commitment.title_id)
    return CommitmentItem(
        id=commitment.id,
        title_id=commitment.title_id,
        title=title.title if title else "(unknown)",
        kind=title.kind.value if title else "movie",
        poster_url=_poster_url(title.poster_path) if title else None,
        status=commitment.status.value,
        note=commitment.note,
        created_at=commitment.created_at,
        resolved_at=commitment.resolved_at,
    )


def _note_item(note: MemoryNote) -> MemoryNoteItem:
    return MemoryNoteItem(
        id=note.id,
        text=note.text,
        kind=note.kind.value,
        expires_at=note.expires_at,
        source=note.source,
        created_at=note.created_at,
    )


@router.get("/profiles/{profile_id}/commitments", response_model=CommitmentList)
def list_commitments(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> CommitmentList:
    require_profile(session, profile_id)
    rows = commitments_store.list_commitments(session, profile_id)
    return CommitmentList(items=[_commitment_item(session, c) for c in rows])


@router.delete("/profiles/{profile_id}/commitments/{commitment_id}", status_code=204)
def delete_commitment(
    profile_id: uuid.UUID,
    commitment_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> None:
    require_profile(session, profile_id)
    commitment = commitments_store.get_commitment(session, commitment_id)
    if commitment is None or commitment.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Commitment not found")
    commitments_store.delete_commitment(session, commitment)
    session.commit()


@router.get("/profiles/{profile_id}/memory", response_model=MemoryNoteList)
def list_memory(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> MemoryNoteList:
    require_profile(session, profile_id)
    return MemoryNoteList(
        items=[_note_item(n) for n in memory_store.list_notes(session, profile_id)]
    )


@router.post("/profiles/{profile_id}/memory", response_model=MemoryNoteItem)
def add_memory(
    profile_id: uuid.UUID,
    body: CreateMemoryNoteRequest,
    session: Annotated[Session, Depends(get_session)],
) -> MemoryNoteItem:
    require_profile(session, profile_id)
    try:
        kind = MemoryKind(body.kind)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Unknown memory kind: {body.kind}") from exc
    note = memory_store.create_note(
        session, profile_id, body.text, kind=kind, expires_at=body.expires_at, source="manual"
    )
    session.commit()
    return _note_item(note)


@router.delete("/profiles/{profile_id}/memory/{note_id}", status_code=204)
def delete_memory(
    profile_id: uuid.UUID,
    note_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> None:
    require_profile(session, profile_id)
    note = memory_store.get_note(session, note_id)
    if note is None or note.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Note not found")
    memory_store.delete_note(session, note)
    session.commit()
