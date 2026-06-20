"""Structured intent + reply shapes for the chat agent."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from phare.recommend.schema import Recommendation


class ChatIntent(BaseModel):
    """Ephemeral mood/intent parsed from a chat message — extra filters over the engine."""

    max_runtime: int | None = None
    include_genres: list[str] = []
    exclude_genres: list[str] = []
    mood: str | None = None


class ToolCall(BaseModel):
    """One step the planner asks for: a tool name plus its arguments."""

    tool: str
    args: dict[str, Any] = {}


class AgentPlan(BaseModel):
    """The planner's decision: which tools to run for this message, in order."""

    calls: list[ToolCall] = []


class AgentAction(BaseModel):
    """A write the agent performed, surfaced to the user and reversible (auto-write + undo)."""

    kind: str  # logged_signal | commitment | resolved | memory | taste
    summary: str  # human-readable: "logged Dune as loved"
    undo_token: str | None = None  # e.g. "event:<id>" or "commitment:<id>,event:<id>"


class ChatReply(BaseModel):
    """The agent's answer: a reply, the recommendations it surfaced, and any writes it made."""

    reply_text: str
    intent: ChatIntent
    items: list[Recommendation]
    actions: list[AgentAction] = []
