"""Chat agent endpoints: message in (reply + recs + writes), undo, and the opening follow-up."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from phare.agent import commitments as commitments_store
from phare.agent.intent import keyword_intent
from phare.agent.service import ChatService, PreparedTurn, stream_compose
from phare.agent.tools import undo_action
from phare.api.deps import (
    Embedder,
    get_embedder,
    get_language,
    get_optional_agent_llm,
    get_optional_chat_llm,
)
from phare.api.recommend import build_recommender, require_profile, to_item
from phare.api.schemas import (
    AgentActionResponse,
    ChatIntentResponse,
    ChatOpeningResponse,
    ChatReplyResponse,
    ChatRequest,
    UndoRequest,
    UndoResponse,
)
from phare.core.auth import get_current_user
from phare.core.i18n import Language
from phare.db.base import get_session
from phare.db.models import Title, User
from phare.providers.types import LLMProvider
from phare.taste.service import maybe_refresh_taste

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Chat"])


@router.post("/profiles/{profile_id}/chat", response_model=ChatReplyResponse)
def chat(
    profile_id: uuid.UUID,
    body: ChatRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
    agent_llm: Annotated[LLMProvider | None, Depends(get_optional_agent_llm)],
    language: Annotated[Language, Depends(get_language)],
) -> ChatReplyResponse:
    require_profile(user, profile_id)
    # Explanations (high volume) use the workhorse model; the conversational agent uses agent_llm.
    recommender = build_recommender(session, embedder, chat_llm, language)
    reply = ChatService(recommender, agent_llm).respond(profile_id, body.message)
    session.commit()
    return ChatReplyResponse(
        reply_text=reply.reply_text,
        intent=ChatIntentResponse(
            max_runtime=reply.intent.max_runtime,
            include_genres=reply.intent.include_genres,
            exclude_genres=reply.intent.exclude_genres,
            mood=reply.intent.mood,
        ),
        items=[to_item(item) for item in reply.items],
        actions=[
            AgentActionResponse(kind=a.kind, summary=a.summary, undo_token=a.undo_token)
            for a in reply.actions
        ],
        degraded=reply.degraded,
    )


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _planning_label(message: str) -> str:
    """A friendly, instant acknowledgement of the request — a keyword parse (no LLM), so it can be
    shown the moment the connection opens, while the planner model is still thinking."""
    intent = keyword_intent(message)
    descriptor = ", ".join(intent.include_genres).lower()
    runtime = f" under {intent.max_runtime} min" if intent.max_runtime else ""
    if descriptor or runtime:
        return f"Finding something {descriptor}{runtime}…".replace(" …", "…")
    return "Working on it…"


def _meta_payload(prepared: PreparedTurn) -> dict[str, object]:
    return {
        "intent": ChatIntentResponse(
            max_runtime=prepared.intent.max_runtime,
            include_genres=prepared.intent.include_genres,
            exclude_genres=prepared.intent.exclude_genres,
            mood=prepared.intent.mood,
        ).model_dump(by_alias=True, mode="json"),
        "items": [to_item(item).model_dump(by_alias=True, mode="json") for item in prepared.items],
        "actions": [
            AgentActionResponse(kind=a.kind, summary=a.summary, undo_token=a.undo_token).model_dump(
                by_alias=True, mode="json"
            )
            for a in prepared.actions
        ],
        "degraded": prepared.degraded,
    }


@router.post("/profiles/{profile_id}/chat/stream")
def chat_stream(
    profile_id: uuid.UUID,
    body: ChatRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
    agent_llm: Annotated[LLMProvider | None, Depends(get_optional_agent_llm)],
    language: Annotated[Language, Depends(get_language)],
) -> StreamingResponse:
    """The chat turn as Server-Sent Events, surfacing each step the moment it's ready so the screen
    is never blank: a ``status`` event right away (an instant keyword-based acknowledgement, while
    the planner runs), then ``meta`` with the picks + write-chips as soon as the tools finish, a
    ``status`` while the reply is composed, the reply itself as ``delta`` chunks, then ``done``.
    The DB work (incl. the commit) happens inside the generator; the streamed reply is read-only."""
    require_profile(user, profile_id)
    recommender = build_recommender(session, embedder, chat_llm, language)
    service = ChatService(recommender, agent_llm)

    def events() -> Iterator[str]:
        # Instant: echo the request so the screen isn't blank during the planner call.
        yield _sse("status", {"stage": "planning", "label": _planning_label(body.message)})
        try:
            prepared = service.prepare(profile_id, body.message)
            session.commit()  # persist signals/commitments/memory + logs before the read-only reply
        except Exception:  # noqa: BLE001 - a failed turn must still close the stream gracefully
            logger.exception("chat.stream_prepare_failed")
            yield _sse("delta", {"text": "Sorry — something went wrong. Please try again."})
            yield _sse("done", {})
            return
        yield _sse("meta", _meta_payload(prepared))  # picks + write-chips, the moment they exist
        yield _sse("status", {"stage": "composing", "label": "Writing a reply…"})
        for chunk in stream_compose(prepared, agent_llm):
            yield _sse("delta", {"text": chunk})
        yield _sse("done", {})

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/profiles/{profile_id}/chat/undo", response_model=UndoResponse)
def undo(
    profile_id: uuid.UUID,
    body: UndoRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
    language: Annotated[Language, Depends(get_language)],
) -> UndoResponse:
    """Reverse a write the agent made (auto-write + undo). Re-derives taste afterwards."""
    require_profile(user, profile_id)
    undone = undo_action(session, profile_id, body.token)
    if undone:
        maybe_refresh_taste(session, profile_id, chat_llm, language)
    session.commit()
    return UndoResponse(undone=undone)


@router.get("/profiles/{profile_id}/chat/opening", response_model=ChatOpeningResponse)
def opening(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> ChatOpeningResponse:
    """A proactive follow-up when the user has open watch plans (the cross-session memory hook)."""
    require_profile(user, profile_id)
    pending = commitments_store.pending_commitments(session, profile_id)
    names = [t.title for c in pending if (t := session.get(Title, c.title_id)) is not None]
    if not names:
        return ChatOpeningResponse(greeting=None)
    if len(names) == 1:
        greeting = f"Last time you said you'd watch {names[0]} — did you get to it? How was it?"
    else:
        listed = ", ".join(names[:-1]) + f" or {names[-1]}"
        greeting = (
            f"You had a few on your list — did you watch {listed}? Let me know how they were."
        )
    return ChatOpeningResponse(greeting=greeting)
