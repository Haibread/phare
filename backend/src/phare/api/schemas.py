"""Wire DTOs. Domain/ORM models never get serialized directly; map to these explicitly.

All JSON fields are camelCase on the wire (alias generator), populatable by field name.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class HealthResponse(ApiModel):
    status: str
    service: str
    version: str


class LoginRequest(ApiModel):
    password: str = Field(min_length=1)


class TokenResponse(ApiModel):
    token: str


class MeResponse(ApiModel):
    auth_required: bool
    authenticated: bool


class CreateProfileRequest(ApiModel):
    display_name: str = Field(min_length=1, max_length=100)


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
    components: dict[str, float]


class RecommendationRow(ApiModel):
    key: str
    title: str
    items: list[RecommendationItem]


class RecommendationsResponse(ApiModel):
    rows: list[RecommendationRow]


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


class ChatRequest(ApiModel):
    message: str = Field(min_length=1, max_length=500)


class ChatIntentResponse(ApiModel):
    max_runtime: int | None = None
    include_genres: list[str]
    exclude_genres: list[str]
    mood: str | None = None


class ChatReplyResponse(ApiModel):
    reply_text: str
    intent: ChatIntentResponse
    items: list[RecommendationItem]


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
