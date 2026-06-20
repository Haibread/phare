"""Chat agent service.

Two modes, by config (graceful degradation, principle 5):
- **offline** (no chat LLM): keyword intent → single `recommend` (read-only) — the original
  behavior, since resolving "I saw <something>" to a catalog title needs the model.
- **tool-using** (chat LLM present): planner picks tools → deterministic execution (writes
  signals/commitments/memory) → composed reply. The LLM steers; the engine ranks.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from phare.agent import planner
from phare.agent.intent import parse_intent
from phare.agent.schema import ChatIntent, ChatReply
from phare.agent.tools import ToolContext, execute_plan
from phare.core.config import get_settings
from phare.providers.tmdb import TMDBMetadataProvider
from phare.providers.types import LLMProvider
from phare.recommend.log import log_chat
from phare.recommend.schema import Candidate
from phare.recommend.service import RecommendationService

logger = logging.getLogger(__name__)


def intent_filter(intent: ChatIntent):
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
    """Deterministic reply used in the offline (no-LLM) path."""
    if count == 0:
        return "I couldn't find a good match for that — try loosening the constraints a little."
    bits: list[str] = []
    if intent.include_genres:
        bits.append(", ".join(intent.include_genres).lower())
    descriptor = f"{' '.join(bits)} " if bits else ""
    runtime = f" under {intent.max_runtime} minutes" if intent.max_runtime else ""
    return f"Here are a few {descriptor}picks{runtime} you might enjoy."


class ChatService:
    """Turns a chat message into a reply + recommendations (and, with an LLM, structured writes)."""

    def __init__(self, recommender: RecommendationService, chat_llm: LLMProvider | None) -> None:
        self.recommender = recommender
        self.chat_llm = chat_llm

    def respond(
        self, profile_id: uuid.UUID, message: str, *, now: datetime | None = None
    ) -> ChatReply:
        self.recommender.ensure_embeddings()
        if self.chat_llm is None:
            return self._respond_offline(profile_id, message)
        return self._respond_with_tools(profile_id, message, now or datetime.now(UTC))

    def _respond_offline(self, profile_id: uuid.UUID, message: str) -> ChatReply:
        intent = parse_intent(message, None)  # keyword parser; no writes without the LLM
        items = self.recommender.recommend(
            profile_id,
            extra_hard_avoids=intent.exclude_genres,
            candidate_filter=intent_filter(intent),
            swing_slots=1,
        )
        log_chat(self.recommender.session, profile_id, items)
        return ChatReply(reply_text=_reply_text(intent, len(items)), intent=intent, items=items)

    def _respond_with_tools(self, profile_id: uuid.UUID, message: str, now: datetime) -> ChatReply:
        session = self.recommender.session
        settings = get_settings()
        metadata = (
            TMDBMetadataProvider(api_key=settings.tmdb_api_key, base_url=settings.tmdb_base_url)
            if settings.tmdb_api_key
            else None
        )
        ctx = ToolContext(
            session=session,
            profile_id=profile_id,
            recommender=self.recommender,
            now=now,
            metadata=metadata,
            llm=self.chat_llm,
        )
        agent_plan = planner.plan(session, profile_id, message, self.chat_llm, now=now)
        result = execute_plan(ctx, agent_plan)
        if result.items:
            log_chat(session, profile_id, result.items)
        logger.info(
            "agent.respond",
            extra={
                "profile_id": str(profile_id),
                "tool_calls": len(agent_plan.calls),
                "actions": len(result.actions),
                "item_count": len(result.items),
            },
        )
        return ChatReply(
            reply_text=_compose_reply(result),
            intent=result.intent,
            items=result.items,
            actions=result.actions,
        )


def _compose_reply(result) -> str:  # type: ignore[no-untyped-def]
    """Deterministic reply from what the tools did — confirmations, misses, then picks."""
    bits: list[str] = []
    if result.actions:
        bits.append("Got it — " + "; ".join(a.summary for a in result.actions) + ".")
    for note in result.notes:
        bits.append(note[:1].upper() + note[1:] + ".")
    if result.items:
        bits.append("Here are a few picks you might enjoy.")
    elif not result.actions and not result.notes:
        bits.append("I couldn't find a good match — try loosening the constraints a little.")
    return " ".join(bits) if bits else "Done."
