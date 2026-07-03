"""Taste profile: view, generate (LLM), and edit (sticky user overrides)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.deps import get_language
from phare.api.schemas import TasteResponse, UpdateTasteRequest
from phare.core.auth import get_current_user, require_own_profile
from phare.core.config import get_settings
from phare.core.i18n import Language
from phare.db.base import get_session
from phare.db.models import TasteProfile, User
from phare.providers.llm import OpenAILLMProvider
from phare.providers.types import LLMProvider
from phare.taste.service import TasteService, effective_profile

router = APIRouter(tags=["Taste"])


def get_llm_provider() -> LLMProvider:
    """Build the configured LLM provider, or 400 if it isn't set up."""
    settings = get_settings()
    if not settings.llm_api_key:
        raise HTTPException(status_code=400, detail="LLM is not configured (set LLM_API_KEY)")
    return OpenAILLMProvider(
        api_key=settings.llm_api_key,
        chat_model=settings.llm_chat_model,
        embedding_model=settings.llm_embedding_model,
        base_url=settings.llm_base_url,
        monthly_token_budget=settings.llm_monthly_token_budget,
        reasoning_headroom=settings.reasoning_headroom,
    )


def _to_response(taste: TasteProfile) -> TasteResponse:
    return TasteResponse(
        profile_id=taste.profile_id,
        summary=taste.summary_text,
        structured=effective_profile(taste),
        user_overrides=taste.user_overrides,
        confidence=taste.confidence,
        model_version=taste.model_version,
        generated_at=taste.generated_at,
    )


def _require_taste(session: Session, profile_id: uuid.UUID) -> TasteProfile:
    taste = session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))
    if taste is None:
        raise HTTPException(status_code=404, detail="No taste profile yet; generate one first")
    return taste


@router.get("/profiles/{profile_id}/taste", response_model=TasteResponse)
def get_taste(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> TasteResponse:
    require_own_profile(user, profile_id)
    return _to_response(_require_taste(session, profile_id))


@router.post("/profiles/{profile_id}/taste/generate", response_model=TasteResponse)
def generate_taste(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    llm: Annotated[LLMProvider, Depends(get_llm_provider)],
    language: Annotated[Language, Depends(get_language)],
) -> TasteResponse:
    require_own_profile(user, profile_id)
    settings = get_settings()
    # Force-regenerate, but rate-limited per profile: a workhorse LLM call is spent each time, so
    # repeated button clicks must not each trigger one. The auto-refresh on ingest keeps taste fresh
    # between manual regenerations.
    existing = session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))
    if existing is not None and existing.generated_at is not None:
        elapsed = (datetime.now(UTC) - existing.generated_at).total_seconds()
        retry_after = int(settings.taste_generate_cooldown_seconds - elapsed)
        if retry_after > 0:
            minutes, seconds = divmod(retry_after, 60)
            human = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
            raise HTTPException(
                status_code=429,
                detail=f"Taste was just regenerated — try again in {human}.",
                headers={"Retry-After": str(retry_after)},
            )
    taste = TasteService(session, llm, settings.llm_chat_model, language).generate(profile_id)
    session.commit()
    return _to_response(taste)


@router.put("/profiles/{profile_id}/taste", response_model=TasteResponse)
def update_taste(
    profile_id: uuid.UUID,
    body: UpdateTasteRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> TasteResponse:
    require_own_profile(user, profile_id)
    taste = _require_taste(session, profile_id)
    taste.user_overrides = body.user_overrides
    session.commit()
    return _to_response(taste)
