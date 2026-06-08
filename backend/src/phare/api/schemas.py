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
