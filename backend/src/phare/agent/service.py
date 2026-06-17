"""Chat agent service: message -> intent -> engine (with intent filters) -> reply + items."""

from __future__ import annotations

import logging
import uuid

from phare.agent.intent import parse_intent
from phare.agent.schema import ChatIntent, ChatReply
from phare.providers.types import LLMProvider
from phare.recommend.log import log_chat
from phare.recommend.schema import Candidate
from phare.recommend.service import RecommendationService

logger = logging.getLogger(__name__)


def _intent_filter(intent: ChatIntent):
    """Build a candidate filter from an intent. Runtime is a hard cap; genre is best-effort."""

    def apply(candidates: list[Candidate]) -> list[Candidate]:
        result = candidates
        if intent.max_runtime is not None:
            result = [
                c
                for c in result
                if c.runtime_minutes is None or c.runtime_minutes <= intent.max_runtime
            ]
        if intent.include_genres:
            wanted = {g.lower() for g in intent.include_genres}
            matched = [c for c in result if wanted & {g.lower() for g in c.genres}]
            # Don't return nothing just because the catalog is thin — fall back to runtime-only.
            result = matched or result
        return result

    return apply


def _reply_text(intent: ChatIntent, count: int) -> str:
    """Deterministic reply used when no chat LLM is configured."""
    if count == 0:
        return "I couldn't find a good match for that — try loosening the constraints a little."
    bits: list[str] = []
    if intent.include_genres:
        bits.append(", ".join(intent.include_genres).lower())
    descriptor = f"{' '.join(bits)} " if bits else ""
    runtime = f" under {intent.max_runtime} minutes" if intent.max_runtime else ""
    return f"Here are a few {descriptor}picks{runtime} you might enjoy."


class ChatService:
    """Turns a chat message into recommendations over the shared engine."""

    def __init__(self, recommender: RecommendationService, chat_llm: LLMProvider | None) -> None:
        self.recommender = recommender
        self.chat_llm = chat_llm

    def respond(self, profile_id: uuid.UUID, message: str) -> ChatReply:
        intent = parse_intent(message, self.chat_llm)
        self.recommender.ensure_embeddings()
        items = self.recommender.recommend(
            profile_id,
            extra_hard_avoids=intent.exclude_genres,
            candidate_filter=_intent_filter(intent),
            swing_slots=1,
        )
        log_chat(self.recommender.session, profile_id, items)
        reply = _reply_text(intent, len(items))
        logger.info(
            "agent.respond",
            extra={
                "profile_id": str(profile_id),
                "item_count": len(items),
                "max_runtime": intent.max_runtime,
            },
        )
        return ChatReply(reply_text=reply, intent=intent, items=items)
