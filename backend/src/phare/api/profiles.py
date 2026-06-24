"""Profile read + dev sample-data seeding.

A profile is created with its account (strict 1:1 with a :class:`User`), so there is no create
endpoint here — and every read is scoped to the caller's own profile. See ``docs/auth.md``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from phare.api.deps import get_language
from phare.api.schemas import IngestSummary, ProfilePage, ProfileResponse
from phare.core.auth import get_current_user, require_own_profile
from phare.core.config import get_settings
from phare.core.i18n import Language
from phare.db.base import get_session
from phare.db.models import Profile, User
from phare.ingest.sample import seed_sample_data
from phare.taste.service import maybe_refresh_taste, optional_llm_provider

router = APIRouter(tags=["Profiles"])


def _to_response(profile: Profile) -> ProfileResponse:
    return ProfileResponse(
        id=profile.id,
        display_name=profile.display_name,
        created_at=profile.created_at,
    )


@router.get("/profiles", response_model=ProfilePage)
def list_profiles(
    user: Annotated[User, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100, alias="perPage")] = 50,
) -> ProfilePage:
    """The caller's own profile (strict 1:1). Returned as a page so the SPA contract is stable."""
    items = [_to_response(user.profile)] if user.profile is not None else []
    return ProfilePage(items=items, page=page, per_page=per_page, total=len(items))


@router.post("/profiles/{profile_id}/sample-data", response_model=IngestSummary)
def load_sample_data(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    language: Annotated[Language, Depends(get_language)],
) -> IngestSummary:
    """Seed a small demo history (dev only). Lets you try the UI without TMDB/Trakt."""
    settings = get_settings()
    if settings.environment == "production":
        raise HTTPException(status_code=403, detail="Sample data is disabled in production")
    require_own_profile(user, profile_id)
    result = seed_sample_data(session, profile_id)
    if result.created or result.updated:
        maybe_refresh_taste(session, profile_id, optional_llm_provider(), language)
    session.commit()
    return IngestSummary(
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        titles_created=result.titles_created,
    )
