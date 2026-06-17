"""Structured intent + reply shapes for the chat agent."""

from __future__ import annotations

from pydantic import BaseModel

from phare.recommend.schema import Recommendation


class ChatIntent(BaseModel):
    """Ephemeral mood/intent parsed from a chat message — extra filters over the engine."""

    max_runtime: int | None = None
    include_genres: list[str] = []
    exclude_genres: list[str] = []
    mood: str | None = None


class ChatReply(BaseModel):
    """The agent's answer: a short reply plus the recommendations it surfaced."""

    reply_text: str
    intent: ChatIntent
    items: list[Recommendation]
