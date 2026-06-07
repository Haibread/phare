"""Interim Trakt sync endpoint.

Accepts a Trakt access token directly and runs the ingestion pipeline. This is a stop-gap
until the OAuth connect flow + per-profile token storage land (depends on the auth model).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy.orm import Session

from phare.api.schemas import ApiModel, IngestSummary
from phare.core.config import get_settings
from phare.db.base import get_session
from phare.db.models import Profile
from phare.ingest.service import IngestionService
from phare.providers.tmdb import TMDBMetadataProvider
from phare.providers.trakt import TraktSourceProvider

router = APIRouter(tags=["Sync"])


class TraktSyncRequest(ApiModel):
    profile_id: uuid.UUID
    access_token: str = Field(min_length=1)


@router.post("/sources/trakt/sync", response_model=IngestSummary)
def sync_trakt(
    body: TraktSyncRequest,
    session: Annotated[Session, Depends(get_session)],
) -> IngestSummary:
    settings = get_settings()
    if not settings.trakt_client_id or not settings.tmdb_api_key:
        raise HTTPException(
            status_code=400,
            detail="TRAKT_CLIENT_ID and TMDB_API_KEY must be configured to sync",
        )
    if session.get(Profile, body.profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    source = TraktSourceProvider(
        client_id=settings.trakt_client_id,
        access_token=body.access_token,
        base_url=settings.trakt_base_url,
    )
    metadata = TMDBMetadataProvider(api_key=settings.tmdb_api_key, base_url=settings.tmdb_base_url)
    result = IngestionService(session, metadata).ingest(body.profile_id, source.pull())
    session.commit()
    return IngestSummary(
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        titles_created=result.titles_created,
    )
