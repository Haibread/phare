"""Generalist agent memory: free-text notes that colour recommendations without fitting a schema.

A steering input, never a ranking authority (principle 1). Durable ``preference`` notes are
distilled into the taste profile by the tool layer; ``context`` notes usually carry an
``expires_at`` and only colour the active session. This module is pure data-access; the taste
distillation + active-note → filter wiring lives in the agent/taste layers.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from phare.db.models import MemoryKind, MemoryNote


def create_note(
    session: Session,
    profile_id: uuid.UUID,
    text: str,
    *,
    kind: MemoryKind = MemoryKind.fact,
    expires_at: datetime | None = None,
    source: str = "chat",
) -> MemoryNote:
    note = MemoryNote(
        profile_id=profile_id, text=text, kind=kind, expires_at=expires_at, source=source
    )
    session.add(note)
    session.flush()
    return note


def get_note(session: Session, note_id: uuid.UUID) -> MemoryNote | None:
    return session.get(MemoryNote, note_id)


def list_notes(session: Session, profile_id: uuid.UUID) -> list[MemoryNote]:
    return list(
        session.scalars(
            select(MemoryNote)
            .where(MemoryNote.profile_id == profile_id)
            .order_by(MemoryNote.created_at.desc())
        )
    )


def active_notes(session: Session, profile_id: uuid.UUID, *, now: datetime) -> list[MemoryNote]:
    """Notes still in effect — unexpired ones. These are what the agent loads into context."""
    return list(
        session.scalars(
            select(MemoryNote)
            .where(
                MemoryNote.profile_id == profile_id,
                or_(MemoryNote.expires_at.is_(None), MemoryNote.expires_at > now),
            )
            .order_by(MemoryNote.created_at.desc())
        )
    )


def delete_note(session: Session, note: MemoryNote) -> None:
    session.delete(note)
