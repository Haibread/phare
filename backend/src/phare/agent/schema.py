"""Structured intent + reply shapes for the chat agent."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from phare.llm_json import as_str_list
from phare.recommend.schema import Recommendation

# The in-flight conversation is fed back to the planner/composer so the agent can resolve references
# ("even shorter") and not repeat itself — short-term memory, held by the client, not persisted.
# Both bounds keep a long chat from blowing the prompt budget: only the last few turns reach the
# model, each truncated. They live here so the planner and composer format history identically.
_HISTORY_MAX_MESSAGES = 6
_HISTORY_MAX_CHARS = 400


class ChatMessage(BaseModel):
    """One prior turn of the conversation, as fed back into a prompt. Distinct from the wire DTO."""

    role: Literal["user", "agent"]
    text: str


def format_active_intent(intent: ChatIntent) -> str:
    """Render the filters currently in effect into a one-line planner hint, so a refinement ("even
    shorter") can adjust a *number the planner can see* instead of guessing blind. The runtime cap
    in particular lives only in the structured intent — it never reaches the replayed prose — so
    without this the planner can't tighten below the previous cap. Empty intent → empty string."""
    parts: list[str] = []
    if intent.include_genres:
        parts.append(", ".join(intent.include_genres))
    if intent.exclude_genres:
        parts.append("no " + ", ".join(intent.exclude_genres))
    if intent.max_runtime is not None:
        parts.append(f"≤{intent.max_runtime} min")
    if intent.mood:
        parts.append(intent.mood)
    if intent.rewatch:
        parts.append("rewatch")
    return "; ".join(parts)


def format_history(history: list[ChatMessage]) -> str:
    """Render recent turns into a prompt block, bounded to the last few and truncated per message.

    Empty (or all-blank) history → empty string, so callers omit the block entirely rather than
    emit a dangling "Recent conversation:" header.
    """
    lines: list[str] = []
    for msg in history[-_HISTORY_MAX_MESSAGES:]:
        text = msg.text.strip()
        if not text:
            continue
        if len(text) > _HISTORY_MAX_CHARS:
            text = text[:_HISTORY_MAX_CHARS].rstrip() + "…"
        label = "User" if msg.role == "user" else "Assistant"
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


class ChatIntent(BaseModel):
    """Ephemeral mood/intent parsed from a chat message — extra filters over the engine.

    The field values come straight from an LLM tool call, so the validators below absorb the shapes
    a model gets wrong without following the schema to the letter — a scalar returned as a list, a
    runtime written as "90 minutes". A mis-shaped field must never sink the whole recommend turn
    (which is exactly what an uncaught ``ValidationError`` here used to do)."""

    max_runtime: int | None = None
    include_genres: list[str] = []
    exclude_genres: list[str] = []
    mood: str | None = None
    # Movie vs show — a hard candidate filter when set. ``None`` = either (product decision: a user
    # may well want both). From an LLM tool call, so the validator absorbs synonyms and never raises
    # — an unknown value coerces to None rather than sinking the turn.
    kind: Literal["movie", "show"] | None = None
    # A rewatch flips the candidate source: draw from titles the profile has already watched/loved
    # instead of excluding them. "a comfort rewatch", "something I've seen", "watch again".
    rewatch: bool = False

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, value: Any) -> str | None:
        # film/movie -> movie; series/tv/série -> show; anything unrecognised -> None (no filter).
        if value is None:
            return None
        text = str(value).strip().lower()
        if text in {"movie", "movies", "film", "films"}:
            return "movie"
        if text in {
            "show",
            "shows",
            "series",
            "serie",
            "série",
            "séries",
            "tv",
            "tv show",
            "tv series",
        }:
            return "show"
        return None

    @field_validator("include_genres", "exclude_genres", mode="before")
    @classmethod
    def _coerce_genre_list(cls, value: Any) -> list[str]:
        # A bare "comedy" must become ["comedy"], not ['c','o','m','e','d','y'].
        return as_str_list(value)

    @field_validator("mood", mode="before")
    @classmethod
    def _coerce_mood(cls, value: Any) -> str | None:
        # Models sometimes hand mood back as a list (["light", "funny"]); flatten to one string.
        if value is None or isinstance(value, str):
            return value or None
        if isinstance(value, (list, tuple)):
            joined = ", ".join(str(item).strip() for item in value if str(item).strip())
            return joined or None
        return str(value)

    @field_validator("max_runtime", mode="before")
    @classmethod
    def _coerce_runtime(cls, value: Any) -> int | None:
        # Tolerate "90", "90 minutes", 90.0 — pull the first integer, or give up to None.
        if value is None or isinstance(value, int):
            return value
        match = re.search(r"\d+", str(value))
        return int(match.group()) if match else None


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
    # Quick-reply chips for a clarifying question (empty otherwise) — see docs/agent.md guardrail 5.
    suggestions: list[str] = []
    # The AI couldn't fully process this turn (planner output unparseable) and fell back to a plain
    # recommendation — no writes, no mood parsing. Surfaced so the UI tells the user honestly rather
    # than pretending the agent understood. See docs/agent.md and docs/configuration.md.
    degraded: bool = False
