"""Structured shape the LLM must emit for a taste profile (validated, not free-form)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TasteProfileData(BaseModel):
    """Validated taste profile produced by the LLM from a viewer's history."""

    summary: str
    likes: list[str] = []
    dislikes: list[str] = []
    hard_avoids: list[str] = []
    # genre/keyword/era/tone -> weight in [-1, 1]
    affinities: dict[str, float] = {}
    comfort_axis: str | None = None
    discovery_tolerance: float | None = Field(default=None, ge=0, le=1)
    confidence: float = Field(default=0.5, ge=0, le=1)
