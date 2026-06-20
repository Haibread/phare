"""Watch commitments: the "I'll watch X" intents the chat agent tracks and follows up on.

Pure data-access over :class:`WatchCommitment`. Resolving a commitment into a real signal lives
in the agent tool layer (it needs the engine); here we only persist and query.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.db.models import CommitmentStatus, WatchCommitment


def create_commitment(
    session: Session, profile_id: uuid.UUID, title_id: uuid.UUID, *, note: str | None = None
) -> WatchCommitment:
    commitment = WatchCommitment(profile_id=profile_id, title_id=title_id, note=note)
    session.add(commitment)
    session.flush()
    return commitment


def get_commitment(session: Session, commitment_id: uuid.UUID) -> WatchCommitment | None:
    return session.get(WatchCommitment, commitment_id)


def list_commitments(
    session: Session, profile_id: uuid.UUID, *, status: CommitmentStatus | None = None
) -> list[WatchCommitment]:
    stmt = select(WatchCommitment).where(WatchCommitment.profile_id == profile_id)
    if status is not None:
        stmt = stmt.where(WatchCommitment.status == status)
    return list(session.scalars(stmt.order_by(WatchCommitment.created_at.desc())))


def pending_commitments(session: Session, profile_id: uuid.UUID) -> list[WatchCommitment]:
    """Open commitments — what the agent's opening follow-up is built from."""
    return list_commitments(session, profile_id, status=CommitmentStatus.pending)


def resolve_commitment(
    commitment: WatchCommitment, *, status: CommitmentStatus, resolved_at: datetime
) -> WatchCommitment:
    """Mark a commitment watched/dropped. Writing the resulting signal is the tool layer's job."""
    commitment.status = status
    commitment.resolved_at = resolved_at
    return commitment


def delete_commitment(session: Session, commitment: WatchCommitment) -> None:
    session.delete(commitment)
