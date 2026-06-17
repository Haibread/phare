"""Persist every recommendation shown — closed-loop measurement groundwork."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy.orm import Session

from phare.db.models import RecommendationLog
from phare.recommend.schema import Recommendation, Row

logger = logging.getLogger(__name__)


def _log_items(
    session: Session,
    profile_id: uuid.UUID,
    items: Sequence[Recommendation],
    *,
    row_key: str,
    source: str,
) -> None:
    for rank, item in enumerate(items):
        session.add(
            RecommendationLog(
                profile_id=profile_id,
                title_id=item.title_id,
                row_key=row_key,
                rank=rank,
                score=item.score,
                is_swing=item.is_swing,
                source=source,
            )
        )


def log_rows(session: Session, profile_id: uuid.UUID, rows: Sequence[Row]) -> int:
    """Log every item across rows. Returns the number of rows logged."""
    total = 0
    for row in rows:
        _log_items(session, profile_id, row.items, row_key=row.key, source="row")
        total += len(row.items)
    session.flush()
    logger.debug("recommend.logged", extra={"profile_id": str(profile_id), "logged": total})
    return total


def log_chat(session: Session, profile_id: uuid.UUID, items: Sequence[Recommendation]) -> int:
    """Log chat-surfaced suggestions. Returns the number logged."""
    _log_items(session, profile_id, items, row_key="chat", source="chat")
    session.flush()
    return len(items)
