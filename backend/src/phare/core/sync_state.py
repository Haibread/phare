"""Read/write the per-(profile, source) incremental-sync high-water mark."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.db.models import SyncState


def get_last_synced(session: Session, profile_id: uuid.UUID, source: str) -> datetime | None:
    """The timestamp of the last successful sync for this profile + source, if any."""
    return session.scalar(
        select(SyncState.last_synced_at).where(
            SyncState.profile_id == profile_id, SyncState.source == source
        )
    )


def set_last_synced(session: Session, profile_id: uuid.UUID, source: str, when: datetime) -> None:
    """Upsert the high-water mark for this profile + source."""
    row = session.scalar(
        select(SyncState).where(SyncState.profile_id == profile_id, SyncState.source == source)
    )
    if row is None:
        session.add(SyncState(profile_id=profile_id, source=source, last_synced_at=when))
    else:
        row.last_synced_at = when
    session.flush()
