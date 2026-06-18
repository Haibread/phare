"""Action-provider endpoints (Seerr): connect, availability, request.

Availability + request take internal ``titleId``s and resolve the TMDB id server-side, so the
wire never carries TMDB ids and the frontend stays keyed on what it already has.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy.orm import Session

from phare.api.schemas import ApiModel
from phare.core.config import get_settings
from phare.core.tokens import get_source_token, store_source_token
from phare.db.base import get_session
from phare.db.models import Profile, Title
from phare.providers.seerr import SeerrProvider
from phare.providers.types import ActionProvider, MediaAvailability

router = APIRouter(tags=["Actions"])
_SOURCE = "seerr"


def seerr_provider(session: Session, profile_id: uuid.UUID) -> ActionProvider | None:
    """Build the action provider from per-profile creds (UI-connected), else env defaults."""
    settings = get_settings()
    raw = get_source_token(session, settings, profile_id, _SOURCE)
    if raw:
        try:
            creds = json.loads(raw)
            return SeerrProvider(base_url=creds["baseUrl"], api_key=creds["apiKey"])
        except (json.JSONDecodeError, KeyError):
            return None
    if settings.seerr_base_url and settings.seerr_api_key:
        return SeerrProvider(base_url=settings.seerr_base_url, api_key=settings.seerr_api_key)
    return None


def _require_profile(session: Session, profile_id: uuid.UUID) -> None:
    if session.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")


class ConnectSeerrRequest(ApiModel):
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)


class ConnectResponse(ApiModel):
    connected: bool


class AvailabilityRequest(ApiModel):
    title_ids: list[uuid.UUID]


class AvailabilityResponse(ApiModel):
    configured: bool
    results: dict[str, str]  # titleId -> MediaAvailability value


class RequestTitleRequest(ApiModel):
    title_id: uuid.UUID


class RequestTitleResponse(ApiModel):
    ok: bool
    availability: str


@router.post("/profiles/{profile_id}/sources/seerr/connect", response_model=ConnectResponse)
def connect_seerr(
    profile_id: uuid.UUID,
    body: ConnectSeerrRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ConnectResponse:
    _require_profile(session, profile_id)
    stored = store_source_token(
        session,
        get_settings(),
        profile_id,
        _SOURCE,
        json.dumps({"baseUrl": body.base_url, "apiKey": body.api_key}),
    )
    if not stored:
        raise HTTPException(status_code=400, detail="SECRET_KEY must be set to store credentials")
    session.commit()
    return ConnectResponse(connected=True)


@router.post("/profiles/{profile_id}/availability", response_model=AvailabilityResponse)
def get_availability(
    profile_id: uuid.UUID,
    body: AvailabilityRequest,
    session: Annotated[Session, Depends(get_session)],
) -> AvailabilityResponse:
    """Library availability for the given titles. Empty + ``configured=false`` when no Seerr."""
    _require_profile(session, profile_id)
    provider = seerr_provider(session, profile_id)
    if provider is None:
        return AvailabilityResponse(configured=False, results={})
    results: dict[str, str] = {}
    for title_id in body.title_ids:
        title = session.get(Title, title_id)
        if title is None or title.tmdb_id is None:
            continue
        try:
            results[str(title_id)] = provider.availability(title.tmdb_id, title.kind).value
        except httpx.HTTPError:
            results[str(title_id)] = MediaAvailability.unknown.value
    return AvailabilityResponse(configured=True, results=results)


@router.post("/profiles/{profile_id}/requests", response_model=RequestTitleResponse)
def request_title(
    profile_id: uuid.UUID,
    body: RequestTitleRequest,
    session: Annotated[Session, Depends(get_session)],
) -> RequestTitleResponse:
    _require_profile(session, profile_id)
    provider = seerr_provider(session, profile_id)
    if provider is None:
        raise HTTPException(status_code=400, detail="No Seerr connected")
    title = session.get(Title, body.title_id)
    if title is None or title.tmdb_id is None:
        raise HTTPException(status_code=404, detail="Title not found or has no TMDB id")
    try:
        provider.request_title(title.tmdb_id, title.kind)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Request failed: {exc}") from exc
    return RequestTitleResponse(ok=True, availability=MediaAvailability.queued.value)
