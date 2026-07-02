"""Wire DTOs. Domain/ORM models never get serialized directly; map to these explicitly.

All JSON fields are camelCase on the wire (alias generator), populatable by field name.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class HealthResponse(ApiModel):
    status: str
    service: str
    version: str


class RegisterRequest(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(ApiModel):
    token: str


class UserResponse(ApiModel):
    id: uuid.UUID
    email: str | None = None
    display_name: str
    is_admin: bool
    profile_id: uuid.UUID


class RegisterResponse(ApiModel):
    # ``token`` is set when self-registering (first-run / open registration); null when an admin
    # creates an account for someone else.
    token: str | None = None
    user: UserResponse


class MeResponse(ApiModel):
    # ``needs_setup``: no account exists yet — the SPA shows first-run setup. ``user`` is the caller
    # when authenticated, else null. See docs/auth.md.
    needs_setup: bool
    registration_open: bool
    authenticated: bool
    user: UserResponse | None = None


class PlexStartResponse(ApiModel):
    challenge_id: str
    auth_url: str


class PlexPollRequest(ApiModel):
    challenge_id: str = Field(min_length=1)


class PlexPollResponse(ApiModel):
    status: str  # pending | authorized | expired
    token: str | None = None  # set only when status is "authorized"


class ProfileResponse(ApiModel):
    id: uuid.UUID
    display_name: str
    created_at: datetime


class ProfilePage(ApiModel):
    items: list[ProfileResponse]
    page: int
    per_page: int
    total: int


class IngestSummary(ApiModel):
    created: int
    updated: int
    skipped: int
    titles_created: int


class TasteResponse(ApiModel):
    profile_id: uuid.UUID
    summary: str | None = None
    structured: dict[str, Any]
    user_overrides: dict[str, Any]
    confidence: float | None = None
    model_version: str | None = None
    generated_at: datetime | None = None


class UpdateTasteRequest(ApiModel):
    user_overrides: dict[str, Any]


class CatalogSummary(ApiModel):
    created: int


class EmbedSummary(ApiModel):
    embedded: int


class RecommendationItem(ApiModel):
    title_id: uuid.UUID
    title: str
    kind: str
    year: int | None = None
    genres: list[str]
    score: float
    is_swing: bool
    confidence: float | None = None
    explanation: str | None = None
    poster_url: str | None = None
    components: dict[str, float]
    watched: bool = False  # the profile has already seen this — the card shows a "Watched" badge


class TitleDetail(ApiModel):
    """Extra metadata for a title's "more info" view — the synopsis/runtime a card doesn't carry."""

    title_id: uuid.UUID
    title: str
    kind: str
    year: int | None = None
    runtime_minutes: int | None = None
    genres: list[str]
    overview: str | None = None
    poster_url: str | None = None
    tmdb_url: str | None = None
    imdb_url: str | None = None


class RecommendationRow(ApiModel):
    key: str
    title: str
    items: list[RecommendationItem]


class RecommendationsResponse(ApiModel):
    rows: list[RecommendationRow]
    # True when a configured LLM couldn't name today's themed rows and the deterministic fallback
    # filled in — the client flags "basic picks" instead of passing them off as AI-curated. Always
    # False for the static home rows (they're deterministic by design).
    degraded: bool = False
    # True when retrieval is running on the local hash embedder (no embedding key): similarity is
    # not semantically meaningful, so the client shows an honest banner and caps the fit label
    # (review M2). False whenever a real embedding provider is configured.
    embeddings_degraded: bool = False
    # True when the profile has history but its taste centroid isn't ready yet (titles still
    # embedding) — the client shows a "building your profile" state instead of a bare page (A12).
    profile_building: bool = False


class RecommendationLogItem(ApiModel):
    id: uuid.UUID
    title_id: uuid.UUID
    row_key: str
    rank: int
    score: float | None = None
    is_swing: bool
    source: str
    shown_at: datetime


class RecommendationLogPage(ApiModel):
    items: list[RecommendationLogItem]
    page: int
    per_page: int
    total: int


class ConversionResponse(ApiModel):
    shown: int
    converted: int
    rate: float | None = None
    swing_shown: int
    swing_converted: int
    swing_rate: float | None = None
    top_k: int
    within_days: int


class ChatHistoryMessage(ApiModel):
    """One prior turn the client replays so the agent isn't a cold start. The conversation lives in
    the client (sessionStorage); the server keeps no transcript. Bounded server-side before it ever
    reaches a prompt (see ``agent.schema.format_history``)."""

    role: Literal["user", "agent"]
    text: str = Field(min_length=1, max_length=500)


class ChatIntentInput(ApiModel):
    """The filters currently in effect, replayed by the client so a follow-up ("even shorter") can
    refine them. The runtime ceiling lives only here, never in the replayed prose, so the planner
    needs this to tighten below the previous cap rather than guess."""

    max_runtime: int | None = None
    include_genres: list[str] = Field(default_factory=list)
    exclude_genres: list[str] = Field(default_factory=list)
    mood: str | None = None


class ChatRequest(ApiModel):
    message: str = Field(min_length=1, max_length=500)
    # Recent conversation, oldest-first. Optional (defaults empty) so the turn stays backward
    # compatible; capped so a client can't balloon the prompt with an unbounded transcript.
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=50)
    # The filters in effect from earlier turns, so a refinement adjusts them instead of restarting.
    active_intent: ChatIntentInput | None = None


class ChatIntentResponse(ApiModel):
    max_runtime: int | None = None
    include_genres: list[str]
    exclude_genres: list[str]
    mood: str | None = None


class AgentActionResponse(ApiModel):
    kind: str
    summary: str
    undo_token: str | None = None


class ChatReplyResponse(ApiModel):
    reply_text: str
    intent: ChatIntentResponse
    items: list[RecommendationItem]
    actions: list[AgentActionResponse] = []
    # Tappable quick-replies when the agent asked a clarifying question (empty on a normal turn).
    suggestions: list[str] = []
    # The AI fell back to a plain recommendation because it couldn't parse the planner output — no
    # writes, no mood parsing. The client shows an honest "reduced mode" note rather than implying
    # the agent fully understood.
    degraded: bool = False


class UndoRequest(ApiModel):
    token: str = Field(min_length=1)


class UndoResponse(ApiModel):
    undone: bool


class FeedbackSignal(enum.StrEnum):
    """A correction a user can send from a recommendation card. One negative member for now — no
    thumbs-up, to avoid optimising for engagement (review K2) — but shaped to grow."""

    not_interested = "not_interested"


class FeedbackRequest(ApiModel):
    # An unknown signal fails enum validation → 422, as the contract requires.
    signal: FeedbackSignal


class FeedbackResponse(ApiModel):
    title_id: uuid.UUID
    signal: FeedbackSignal
    # Same undo mechanism as the chat writes: POST this token to /chat/undo to reverse the signal.
    undo_token: str


class ChatOpeningResponse(ApiModel):
    greeting: str | None = None  # a follow-up prompt when there are open watch plans


class TraktConnectStartResponse(ApiModel):
    device_code: str
    user_code: str
    verification_url: str
    interval: int
    expires_in: int


class TraktConnectPollRequest(ApiModel):
    profile_id: uuid.UUID
    device_code: str = Field(min_length=1)


class TraktConnectStatusResponse(ApiModel):
    status: str  # pending | connected | slow_down | expired | denied


class HistoryItemResponse(ApiModel):
    id: uuid.UUID
    title_id: uuid.UUID
    title: str
    kind: str
    type: str
    rating: float | None = None
    occurred_at: datetime | None = None
    season_number: int | None = None
    episode_number: int | None = None
    source: str
    excluded: bool


class HistoryPage(ApiModel):
    items: list[HistoryItemResponse]
    page: int
    per_page: int
    total: int


class CommitmentItem(ApiModel):
    id: uuid.UUID
    title_id: uuid.UUID
    title: str
    kind: str
    poster_url: str | None = None
    status: str
    note: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class CommitmentList(ApiModel):
    items: list[CommitmentItem]


class MemoryNoteItem(ApiModel):
    id: uuid.UUID
    text: str
    kind: str
    expires_at: datetime | None = None
    source: str
    created_at: datetime


class MemoryNoteList(ApiModel):
    items: list[MemoryNoteItem]


class CreateMemoryNoteRequest(ApiModel):
    text: str = Field(min_length=1, max_length=500)
    kind: str = "fact"
    expires_at: datetime | None = None
