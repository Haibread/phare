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
    # A rewatch flips the candidate source: draw from titles the profile has already watched/loved
    # instead of excluding them. "a comfort rewatch", "something I've seen", "watch again".
    rewatch: bool = False


class ToolCall(BaseModel):
    """One step the planner asks for: a tool name plus its arguments."""

    tool: str
    args: dict[str, Any] = {}


class AgentPlan(BaseModel):
    """The planner's decision: which tools to run for this message, in order."""

    calls: list[ToolCall] = []
    # True when this plan is a *fallback* the planner produced because the model's output couldn't
    # be parsed (not a real decision) — surfaced so the UI can honestly flag "running in reduced
    # mode" instead of silently degrading. See ChatReply.degraded.
    degraded: bool = False


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
    # The AI couldn't fully process this turn (planner output unparseable) and fell back to a plain
    # recommendation — no writes, no mood parsing. Surfaced so the UI tells the user honestly rather
    # than pretending the agent understood. See docs/agent.md and docs/configuration.md.
    degraded: bool = False
