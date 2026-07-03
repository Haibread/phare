"""Profile read + dev sample-data seeding.

A profile is created with its account (strict 1:1 with a :class:`User`), so there is no create
endpoint here — and every read is scoped to the caller's own profile. See ``docs/auth.md``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.deps import get_language
from phare.api.schemas import IngestSummary, OnboardingStatus, ProfilePage, ProfileResponse
from phare.core.auth import get_current_user, require_own_profile
from phare.core.config import get_settings
from phare.core.i18n import Language
from phare.db.base import get_session
from phare.db.models import Profile, TasteProfile, Title, User, WatchEvent
from phare.ingest.sample import seed_sample_data
from phare.taste.backfill import schedule_taste_refresh
from phare.taste.service import optional_llm_provider

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
    """Seed a small demo history (dev only). Lets you try the UI without TMDB/Trakt.

    Taste extraction is the slow part (an LLM pass), so it's handed to a background refresh and the
    response returns as soon as the history is seeded — the caller lands in the app and polls
    onboarding status while taste fills in (review C3)."""
    settings = get_settings()
    if settings.environment == "production":
        raise HTTPException(status_code=403, detail="Sample data is disabled in production")
    require_own_profile(user, profile_id)
    result = seed_sample_data(session, profile_id)
    # Commit the history *before* scheduling: the background refresh runs on its own connection and
    # must be able to see the events.
    session.commit()
    taste_building = False
    if result.created or result.updated:
        taste_building = schedule_taste_refresh(
            profile_id, optional_llm_provider(), settings.llm_chat_model, language
        )
    return IngestSummary(
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        titles_created=result.titles_created,
        taste_building=taste_building,
    )


@router.get("/profiles/{profile_id}/onboarding", response_model=OnboardingStatus)
def get_onboarding_status(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> OnboardingStatus:
    """Ordered readiness of a fresh profile, for the cold-start screen's step display.

    Read straight from the data: a catalog exists, history is seeded, taste is extracted. The app
    can land once the catalog + history are ready; taste finishes in the background."""
    require_own_profile(user, profile_id)
    catalog_ready = session.scalar(select(Title.id).limit(1)) is not None
    history_ready = (
        session.scalar(
            select(WatchEvent.id)
            .where(WatchEvent.profile_id == profile_id, WatchEvent.excluded.is_(False))
            .limit(1)
        )
        is not None
    )
    taste_ready = (
        session.scalar(
            select(TasteProfile.id).where(TasteProfile.profile_id == profile_id).limit(1)
        )
        is not None
    )
    # Offline, taste never extracts (the deterministic centroid personalises instead) — so the step
    # is "done" the moment history lands, or the flow would stall on a step that can't complete.
    if optional_llm_provider() is None:
        taste_ready = history_ready
    return OnboardingStatus(
        catalog_ready=catalog_ready,
        history_ready=history_ready,
        taste_ready=taste_ready,
        ready_to_browse=catalog_ready and history_ready,
    )
