"""Chat agent endpoint: a message in, a reply + recommendations out."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from phare.agent.service import ChatService
from phare.api.deps import Embedder, get_embedder, get_optional_chat_llm
from phare.api.recommend import build_recommender, require_profile, to_item
from phare.api.schemas import ChatIntentResponse, ChatReplyResponse, ChatRequest
from phare.db.base import get_session
from phare.providers.types import LLMProvider

router = APIRouter(tags=["Chat"])


@router.post("/profiles/{profile_id}/chat", response_model=ChatReplyResponse)
def chat(
    profile_id: uuid.UUID,
    body: ChatRequest,
    session: Annotated[Session, Depends(get_session)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
) -> ChatReplyResponse:
    require_profile(session, profile_id)
    recommender = build_recommender(session, embedder, chat_llm)
    reply = ChatService(recommender, chat_llm).respond(profile_id, body.message)
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
    )
