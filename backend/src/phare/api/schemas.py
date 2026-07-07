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


# Password policy (review I4): length is the only rule — long beats a mandated-symbol short one, and
# arbitrary complexity rules push users to predictable patterns. 128 caps the argon2 input.
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128


class RegisterRequest(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class TokenResponse(ApiModel):
    token: str


class ChangePasswordRequest(ApiModel):
    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class AdminResetPasswordResponse(ApiModel):
    user_id: uuid.UUID
    # A one-off temporary password the admin hands to the user; they should change it after login.
    temporary_password: str


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
    # True when taste extraction was handed to a background pass (the caller can land now and poll
    # onboarding status). False when nothing to extract — offline, or no new events.
    taste_building: bool = False


class SyncPartialFailure(ApiModel):
    """Error body when a source sync dies partway through, after some batches were committed.

    The batches already committed stay in the DB (the upsert is idempotent), so re-running the sync
    resumes where it stopped without duplicates — the UI tells the user exactly that instead of a
    bare "sync failed" (review G3). ``code`` is a stable discriminant the frontend keys on;
    ``ingested`` is how many watch events durably landed before the failure."""

    code: Literal["sync_partial_failure"] = "sync_partial_failure"
    ingested: int


class LLMUnavailable(ApiModel):
    """Error body when a manual, user-requested LLM call can't be served — the provider is
    unreachable (transport/HTTP error) or the monthly token budget is spent.

    Only the *manual* ``POST /taste/generate`` returns this: the user explicitly asked for an LLM
    regeneration, so honesty wins over silently falling back to the deterministic profile (principle
    #4). The automatic taste refresh, by contrast, swallows the same failure by design. ``code`` is
    the stable discriminant the frontend keys on to show a localized, actionable message."""

    code: Literal["llm_unavailable"] = "llm_unavailable"
    reason: Literal["llm_unreachable", "budget_exhausted"]


class OnboardingStatus(ApiModel):
    """Where a fresh profile is in getting ready, as ordered steps the UI can show.

    Derived from actual DB state (titles / events / taste exist), not a bookkeeping flag — so it's
    correct after a restart and can't drift from reality. ``ready_to_browse`` gates landing: the app
    reveals once the catalog + history exist, while taste finishes in the background (review C3)."""

    catalog_ready: bool
    history_ready: bool
    taste_ready: bool
    ready_to_browse: bool


class TasteResponse(ApiModel):
    profile_id: uuid.UUID
    summary: str | None = None
    structured: dict[str, Any]
    # Display-only localization of the free-form chips in `structured`: canonical value → label in
    # the request's language. The canonical values never change (overrides key on them, review F1);
    # the frontend prefers this map, then its static closed-vocab table, then the canonical string.
    # Empty when nothing needed translating (same language, offline, or write endpoints — the
    # subsequent GET refetch carries the map).
    display_terms: dict[str, str] = Field(default_factory=dict)
    user_overrides: dict[str, Any]
    confidence: float | None = None
    model_version: str | None = None
    generated_at: datetime | None = None


class UpdateTasteRequest(ApiModel):
    user_overrides: dict[str, Any]


class FacetExemplarResponse(ApiModel):
    """A member title most central to a taste facet — shown as the facet's face on the Profile."""

    title_id: uuid.UUID
    title: str
    year: int | None = None
    poster_url: str | None = None


class TasteFacetResponse(ApiModel):
    """One taste mode, made inspectable (principle 2): a deterministic genre label, its share of
    the positive event mass (``weight``, facets sum to 1), how many titles seeded it, and the 3
    most central member titles. No LLM anywhere — pure clustering + genre counting."""

    label: str
    weight: float
    title_count: int
    exemplars: list[FacetExemplarResponse]


class TasteFacetsResponse(ApiModel):
    """Weight-descending facets; empty when the taste has a single mode (no insight to show)."""

    facets: list[TasteFacetResponse]


class CatalogSummary(ApiModel):
    created: int


class EmbedSummary(ApiModel):
    embedded: int


class RecommendationItem(ApiModel):
    title_id: uuid.UUID
    title: str
    # Localized display name from the title_localization cache ("Amour éternel" for Kara Sevda),
    # set when the request language isn't English and a cached localization exists; null otherwise.
    # Additive: `title` stays canonical (embedding space + genre vocabulary are language-neutral),
    # the frontend shows `displayTitle ?? title`. Filled in bulk — never a per-card TMDB fetch.
    display_title: str | None = None
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
    # TMDB known-ness/quality signals — filled by search results so the cards can show a compact
    # "★ 8.4 · 37k" rating; null (and hidden) on home rows, which carry the fit gauge instead.
    vote_average: float | None = None
    vote_count: int | None = None


class TitleDetail(ApiModel):
    """Extra metadata for a title's "more info" view — the synopsis/runtime a card doesn't carry."""

    title_id: uuid.UUID
    title: str
    # Localized display name (same contract as RecommendationItem.display_title): non-null only
    # when the request language isn't English and a localization is cached or freshly fetched.
    display_title: str | None = None
    kind: str
    year: int | None = None
    runtime_minutes: int | None = None
    genres: list[str]
    overview: str | None = None
    poster_url: str | None = None
    tmdb_url: str | None = None
    imdb_url: str | None = None
    # Richer credits/quality metadata (round 8). Empty arrays / null until the metadata heal has
    # visited the title; the frontend hides an empty section rather than showing a blank line.
    directors: list[str] = []
    top_cast: list[str] = []
    vote_average: float | None = None
    vote_count: int | None = None
    original_language: str | None = None


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
    """A signal a user can send about a title from the UI.

    ``not_interested`` is the negative card correction — no on-card thumbs-up, to avoid optimising
    for engagement (review K2). ``loved`` is the onboarding "start from scratch" seed: a title the
    user picks as one they loved, written as a watched+liked pair so a taste profile can be built
    with no connected history (round 7, fix 1). It is deliberately not exposed on recommendation
    cards — it's an onboarding-only positive signal."""

    not_interested = "not_interested"
    loved = "loved"


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
