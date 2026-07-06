"""Card-level feedback: a lightweight negative signal ("not interested") from a recommendation card.

Writes through the same taste pipeline as the chat agent's signal tool and reuses its undo mechanism
verbatim (an ``event:<id>`` token, reversed via ``/chat/undo``). The written event both teaches the
taste profile (a negative contribution) and, being an event on the title, keeps that title out of
future candidate generation — the per-title hard-avoid the review asked for (K2).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from phare.api.deps import get_language, get_optional_chat_llm
from phare.api.recommend import require_profile
from phare.api.schemas import FeedbackRequest, FeedbackResponse, FeedbackSignal
from phare.core.auth import get_current_user
from phare.core.i18n import Language
from phare.db.base import get_session
from phare.db.models import EventType, Title, User, WatchEvent
from phare.providers.types import LLMProvider
from phare.taste.service import maybe_refresh_taste

router = APIRouter()

# API signal → the event(s) we persist. Most signals are one event; ``loved`` stacks watched+liked
# (mirroring the chat signal tool's "loved" mapping) so an onboarding seed both teaches the centroid
# and marks the title as seen. The map is where new signals plug in.
_SIGNAL_EVENTS: dict[FeedbackSignal, tuple[EventType, ...]] = {
    FeedbackSignal.not_interested: (EventType.not_interested,),
    FeedbackSignal.loved: (EventType.watched, EventType.liked),
}


@router.post("/profiles/{profile_id}/titles/{title_id}/feedback", response_model=FeedbackResponse)
def submit_feedback(
    profile_id: uuid.UUID,
    title_id: uuid.UUID,
    body: FeedbackRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
    language: Annotated[Language, Depends(get_language)],
) -> FeedbackResponse:
    """Record a card feedback signal, re-derive taste, and return an undo token.

    404 if the title doesn't exist; 422 (via enum validation) if the signal is unknown.
    """
    require_profile(user, profile_id)
    if session.get(Title, title_id) is None:
        raise HTTPException(status_code=404, detail="Title not found")

    now = datetime.now(UTC)  # a fresh signal counts at full recency weight
    event_ids: list[uuid.UUID] = []
    for event_type in _SIGNAL_EVENTS[body.signal]:
        event = WatchEvent(
            profile_id=profile_id,
            title_id=title_id,
            type=event_type,
            occurred_at=now,
            source="feedback",
            external_ref=f"feedback:{uuid.uuid4()}",
        )
        session.add(event)
        session.flush()
        event_ids.append(event.id)
    # Same token shape the chat signal tool mints (comma-joined for multi-event signals), so
    # /chat/undo reverses each with no special-casing.
    undo_token = ",".join(f"event:{event_id}" for event_id in event_ids)
    maybe_refresh_taste(session, profile_id, chat_llm, language)
    session.commit()
    return FeedbackResponse(title_id=title_id, signal=body.signal, undo_token=undo_token)
