"""Profile management + dev sample-data seeding."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.api.schemas import (
    CreateProfileRequest,
    IngestSummary,
    ProfilePage,
    ProfileResponse,
)
from phare.core.config import get_settings
from phare.db.base import get_session
from phare.db.models import Profile
from phare.ingest.sample import seed_sample_data

router = APIRouter(tags=["Profiles"])


def _to_response(profile: Profile) -> ProfileResponse:
    return ProfileResponse(
        id=profile.id,
        display_name=profile.display_name,
        created_at=profile.created_at,
    )


@router.get("/profiles", response_model=ProfilePage)
def list_profiles(
    session: Annotated[Session, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100, alias="perPage")] = 50,
) -> ProfilePage:
    total = session.scalar(select(func.count()).select_from(Profile)) or 0
    rows = session.scalars(
        select(Profile)
        .order_by(Profile.created_at.asc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()
    return ProfilePage(
        items=[_to_response(p) for p in rows],
        page=page,
        per_page=per_page,
        total=total,
    )


@router.post("/profiles", response_model=ProfileResponse, status_code=201)
def create_profile(
    body: CreateProfileRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ProfileResponse:
    profile = Profile(display_name=body.display_name)
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return _to_response(profile)


@router.post("/profiles/{profile_id}/sample-data", response_model=IngestSummary)
def load_sample_data(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> IngestSummary:
    """Seed a small demo history (dev only). Lets you try the UI without TMDB/Trakt."""
    settings = get_settings()
    if settings.environment == "production":
        raise HTTPException(status_code=403, detail="Sample data is disabled in production")
    if session.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    result = seed_sample_data(session, profile_id)
    session.commit()
    return IngestSummary(
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        titles_created=result.titles_created,
    )
