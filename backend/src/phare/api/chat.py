"""Chat agent endpoints: message in (reply + recs + writes), undo, and the opening follow-up."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from phare.agent import commitments as commitments_store
from phare.agent.service import ChatService, stream_compose
from phare.agent.tools import undo_action
from phare.api.deps import (
    Embedder,
    get_embedder,
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
from phare.db.base import get_session
from phare.db.models import Title
from phare.providers.types import LLMProvider
from phare.taste.service import maybe_refresh_taste

router = APIRouter(tags=["Chat"])


@router.post("/profiles/{profile_id}/chat", response_model=ChatReplyResponse)
def chat(
    profile_id: uuid.UUID,
    body: ChatRequest,
    session: Annotated[Session, Depends(get_session)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
    agent_llm: Annotated[LLMProvider | None, Depends(get_optional_agent_llm)],
) -> ChatReplyResponse:
    require_profile(session, profile_id)
    # Explanations (high volume) use the workhorse model; the conversational agent uses agent_llm.
    recommender = build_recommender(session, embedder, chat_llm)
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
    )


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/profiles/{profile_id}/chat/stream")
def chat_stream(
    profile_id: uuid.UUID,
    body: ChatRequest,
    session: Annotated[Session, Depends(get_session)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
    agent_llm: Annotated[LLMProvider | None, Depends(get_optional_agent_llm)],
) -> StreamingResponse:
    """Same turn as ``/chat`` but Server-Sent Events: a ``meta`` event with the picks + writes
    (so the UI can show them immediately), then ``delta`` events streaming the reply text, then
    ``done``. Writes are committed before streaming begins — the stream itself is read-only."""
    require_profile(session, profile_id)
    recommender = build_recommender(session, embedder, chat_llm)
    prepared = ChatService(recommender, agent_llm).prepare(profile_id, body.message)
    session.commit()  # persist signals/commitments/memory + logs before the (read-only) stream

    meta = {
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
    }

    def events() -> Iterator[str]:
        yield _sse("meta", meta)
        for chunk in stream_compose(prepared, agent_llm):
            yield _sse("delta", {"text": chunk})
        yield _sse("done", {})

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/profiles/{profile_id}/chat/undo", response_model=UndoResponse)
def undo(
    profile_id: uuid.UUID,
    body: UndoRequest,
    session: Annotated[Session, Depends(get_session)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
) -> UndoResponse:
    """Reverse a write the agent made (auto-write + undo). Re-derives taste afterwards."""
    require_profile(session, profile_id)
    undone = undo_action(session, profile_id, body.token)
    if undone:
        maybe_refresh_taste(session, profile_id, chat_llm)
    session.commit()
    return UndoResponse(undone=undone)


@router.get("/profiles/{profile_id}/chat/opening", response_model=ChatOpeningResponse)
def opening(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> ChatOpeningResponse:
    """A proactive follow-up when the user has open watch plans (the cross-session memory hook)."""
    require_profile(session, profile_id)
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
