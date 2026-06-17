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
from phare.core.tokens import get_source_token, store_source_token
from phare.db.base import get_session
from phare.db.models import Profile
from phare.ingest.service import IngestionService
from phare.providers.jellyfin import JellyfinSourceProvider
from phare.providers.plex import PlexSourceProvider
from phare.providers.tmdb import TMDBMetadataProvider
from phare.providers.trakt import TraktSourceProvider
from phare.providers.types import SourceProvider

router = APIRouter(tags=["Sync"])


def _require_tmdb(settings: object) -> str:
    """All sources resolve titles through TMDB; fail fast if it isn't configured."""
    key = getattr(settings, "tmdb_api_key", None)
    if not key:
        raise HTTPException(status_code=400, detail="TMDB_API_KEY must be configured to sync")
    return key


def _ingest_from(session: Session, profile_id: uuid.UUID, source: SourceProvider) -> IngestSummary:
    """Shared tail for every source: resolve via TMDB, ingest, commit, summarise."""
    if session.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    settings = get_settings()
    metadata = TMDBMetadataProvider(api_key=settings.tmdb_api_key, base_url=settings.tmdb_base_url)
    result = IngestionService(session, metadata).ingest(profile_id, source.pull())
    session.commit()
    return IngestSummary(
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        titles_created=result.titles_created,
    )


class TraktSyncRequest(ApiModel):
    profile_id: uuid.UUID
    # Optional: when omitted we use a previously stored token (auth/token model).
    access_token: str | None = Field(default=None, min_length=1)


class PlexSyncRequest(ApiModel):
    profile_id: uuid.UUID
    base_url: str = Field(min_length=1)
    token: str | None = Field(default=None, min_length=1)
    account_id: str | None = None


class JellyfinSyncRequest(ApiModel):
    profile_id: uuid.UUID
    base_url: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    api_key: str | None = Field(default=None, min_length=1)


def _resolve_token(
    session: Session, profile_id: uuid.UUID, source: str, supplied: str | None
) -> str:
    """Use the supplied token (and persist it) or fall back to a stored one; else 400."""
    settings = get_settings()
    token = supplied or get_source_token(session, settings, profile_id, source)
    if token is None:
        raise HTTPException(
            status_code=400, detail=f"No {source} token provided or stored for this profile"
        )
    if supplied is not None:
        store_source_token(session, settings, profile_id, source, supplied)
    return token


@router.post("/sources/trakt/sync", response_model=IngestSummary)
def sync_trakt(
    body: TraktSyncRequest,
    session: Annotated[Session, Depends(get_session)],
) -> IngestSummary:
    settings = get_settings()
    if not settings.trakt_client_id:
        raise HTTPException(status_code=400, detail="TRAKT_CLIENT_ID must be configured to sync")
    _require_tmdb(settings)
    token = _resolve_token(session, body.profile_id, "trakt", body.access_token)
    source = TraktSourceProvider(
        client_id=settings.trakt_client_id,
        access_token=token,
        base_url=settings.trakt_base_url,
    )
    return _ingest_from(session, body.profile_id, source)


@router.post("/sources/plex/sync", response_model=IngestSummary)
def sync_plex(
    body: PlexSyncRequest,
    session: Annotated[Session, Depends(get_session)],
) -> IngestSummary:
    """Sync the account owner's own Plex watch history (privacy-safe: single account)."""
    _require_tmdb(get_settings())
    token = _resolve_token(session, body.profile_id, "plex", body.token)
    source = PlexSourceProvider(base_url=body.base_url, token=token, account_id=body.account_id)
    return _ingest_from(session, body.profile_id, source)


@router.post("/sources/jellyfin/sync", response_model=IngestSummary)
def sync_jellyfin(
    body: JellyfinSyncRequest,
    session: Annotated[Session, Depends(get_session)],
) -> IngestSummary:
    """Sync one Jellyfin user's own played history (privacy-safe: single user)."""
    _require_tmdb(get_settings())
    token = _resolve_token(session, body.profile_id, "jellyfin", body.api_key)
    source = JellyfinSourceProvider(base_url=body.base_url, api_key=token, user_id=body.user_id)
    return _ingest_from(session, body.profile_id, source)
