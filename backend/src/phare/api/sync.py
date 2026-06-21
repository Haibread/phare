"""Source sync + connect endpoints (Trakt OAuth device flow, Plex, Jellyfin)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.deps import require_safe_url
from phare.api.schemas import (
    ApiModel,
    IngestSummary,
    TraktConnectPollRequest,
    TraktConnectStartResponse,
    TraktConnectStatusResponse,
)
from phare.core.auth import get_current_user, require_own_profile
from phare.core.config import Settings, get_settings
from phare.core.sync_state import get_last_synced, set_last_synced
from phare.core.tokens import get_source_token, store_source_token
from phare.db.base import get_session
from phare.db.models import Profile, SourceToken, Title, TitleKind, User
from phare.ingest.service import IngestionService, IngestResult
from phare.providers.jellyfin import JellyfinSourceProvider
from phare.providers.plex import PlexSourceProvider
from phare.providers.tmdb import TMDBMetadataProvider
from phare.providers.trakt import TraktSourceProvider
from phare.providers.trakt_oauth import PollStatus, TraktOAuth
from phare.providers.types import MetadataProvider, RawEvent, RawMediaType, SourceProvider
from phare.taste.service import maybe_refresh_taste, optional_llm_provider

router = APIRouter(tags=["Sync"])

# Internal token rows that aren't user-facing "connected sources".
_INTERNAL_SOURCES = {"trakt_refresh"}

# Commit the ingest in batches rather than once at the end, so a long first sync (a full Trakt
# history is thousands of events resolved one-by-one through TMDB) shows up progressively in the UI
# and a mid-sync failure keeps everything ingested so far instead of rolling the whole lot back.
_INGEST_BATCH_SIZE = 100
# Concurrency for warming the TMDB cache. The slow part of a first sync is resolving each new title
# through TMDB one HTTP call at a time; fetching a batch's new titles concurrently (read-only, into
# the shared cache) before the sequential ingest cuts that from minutes to seconds.
_PREWARM_WORKERS = 8


def _prewarm_metadata(session: Session, metadata: MetadataProvider, batch: list[RawEvent]) -> None:
    """Concurrently fetch TMDB metadata for the batch's not-yet-stored titles, warming the shared
    cache. Only the read-side HTTP runs in threads — the DB writes stay on the single session."""
    wanted: dict[int, TitleKind] = {}
    for event in batch:
        if event.tmdb_id is not None:
            kind = TitleKind.movie if event.media_type is RawMediaType.movie else TitleKind.show
            wanted.setdefault(event.tmdb_id, kind)
    if not wanted:
        return
    # Skip titles already in the catalog — the ingest resolves those from the DB, no HTTP needed.
    stored = set(session.scalars(select(Title.tmdb_id).where(Title.tmdb_id.in_(wanted))).all())
    missing = [(tmdb_id, kind) for tmdb_id, kind in wanted.items() if tmdb_id not in stored]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=_PREWARM_WORKERS) as pool:
        # Results land in the provider's shared cache; the sequential ingest below hits it.
        list(pool.map(lambda pair: metadata.get_title(pair[0], pair[1]), missing))


class ConnectedSource(ApiModel):
    source: str
    kind: str  # "history" | "requests"
    last_synced_at: datetime | None = None


def _trakt_oauth(settings: Settings) -> TraktOAuth:
    """Build the Trakt OAuth client, or 400 if the app credentials aren't configured."""
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise HTTPException(
            status_code=400,
            detail="TRAKT_CLIENT_ID and TRAKT_CLIENT_SECRET must be set to connect Trakt",
        )
    return TraktOAuth(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
        base_url=settings.trakt_base_url,
    )


def _require_tmdb(settings: object) -> str:
    """All sources resolve titles through TMDB; fail fast if it isn't configured."""
    key = getattr(settings, "tmdb_api_key", None)
    if not key:
        raise HTTPException(status_code=400, detail="TMDB_API_KEY must be configured to sync")
    return key


def ingest_in_batches(
    session: Session,
    profile_id: uuid.UUID,
    metadata: MetadataProvider,
    events: Iterable[RawEvent],
    *,
    batch_size: int = _INGEST_BATCH_SIZE,
) -> IngestResult:
    """Ingest a stream of events, committing every ``batch_size`` so progress is durable + visible.

    Each committed batch is independently persisted: a failure partway through keeps the batches
    already committed (the upsert is idempotent, so a later re-sync just fills in the rest).
    """
    ingester = IngestionService(session, metadata)
    totals = IngestResult()
    batch: list[RawEvent] = []

    def flush() -> None:
        if not batch:
            return
        _prewarm_metadata(session, metadata, batch)
        part = ingester.ingest(profile_id, batch)
        totals.created += part.created
        totals.updated += part.updated
        totals.skipped += part.skipped
        totals.titles_created += part.titles_created
        session.commit()
        batch.clear()

    for event in events:
        batch.append(event)
        if len(batch) >= batch_size:
            flush()
    flush()
    return totals


def _ingest_from(
    session: Session,
    profile_id: uuid.UUID,
    source: SourceProvider,
    *,
    source_name: str,
    since: datetime | None,
) -> IngestSummary:
    """Shared tail for each source: resolve via TMDB, batch-ingest, set the watermark."""
    if session.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    settings = get_settings()
    # Stamp the watermark from *before* the pull so mid-sync events aren't skipped next time.
    started_at = datetime.now(UTC)
    metadata = TMDBMetadataProvider(
        api_key=settings.tmdb_api_key,
        base_url=settings.tmdb_base_url,
        cache_ttl=settings.tmdb_cache_ttl_seconds,
    )
    result = ingest_in_batches(session, profile_id, metadata, source.pull(since))
    set_last_synced(session, profile_id, source_name, started_at)
    # Taste is derived from history; refresh it automatically when the sync changed anything.
    if result.created or result.updated:
        maybe_refresh_taste(session, profile_id, optional_llm_provider())
    session.commit()
    return IngestSummary(
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        titles_created=result.titles_created,
    )


def _since_for(
    session: Session, profile_id: uuid.UUID, source_name: str, *, full: bool
) -> datetime | None:
    """Incremental cutoff: the last-synced time, unless a full re-sync was requested."""
    return None if full else get_last_synced(session, profile_id, source_name)


class TraktSyncRequest(ApiModel):
    profile_id: uuid.UUID
    # Optional: when omitted we use a previously stored token (auth/token model).
    access_token: str | None = Field(default=None, min_length=1)
    full: bool = False  # force a full re-sync instead of incremental


class PlexSyncRequest(ApiModel):
    profile_id: uuid.UUID
    base_url: str = Field(min_length=1)
    token: str | None = Field(default=None, min_length=1)
    account_id: str | None = None
    full: bool = False


class JellyfinSyncRequest(ApiModel):
    profile_id: uuid.UUID
    base_url: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    api_key: str | None = Field(default=None, min_length=1)
    full: bool = False


def _store_trakt_tokens(
    session: Session,
    settings: Settings,
    profile_id: uuid.UUID,
    access_token: str,
    refresh_token: str | None,
) -> None:
    """Persist the Trakt access token (and refresh token, if given) for a profile."""
    store_source_token(session, settings, profile_id, "trakt", access_token)
    if refresh_token is not None:
        store_source_token(session, settings, profile_id, "trakt_refresh", refresh_token)


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


def _trakt_source(settings: Settings, access_token: str) -> TraktSourceProvider:
    return TraktSourceProvider(
        client_id=settings.trakt_client_id or "",
        access_token=access_token,
        base_url=settings.trakt_base_url,
    )


def _refresh_trakt_token(session: Session, settings: Settings, profile_id: uuid.UUID) -> str:
    """Renew an expired Trakt access token from the stored refresh token, or 401 to reconnect."""
    refresh = get_source_token(session, settings, profile_id, "trakt_refresh")
    if refresh is None:
        raise HTTPException(status_code=401, detail="Trakt token expired — reconnect Trakt")
    result = _trakt_oauth(settings).refresh(refresh)
    if result.status is not PollStatus.connected or result.access_token is None:
        raise HTTPException(status_code=401, detail="Trakt refresh failed — reconnect Trakt")
    _store_trakt_tokens(session, settings, profile_id, result.access_token, result.refresh_token)
    return result.access_token


@router.post("/sources/trakt/sync", response_model=IngestSummary)
def sync_trakt(
    body: TraktSyncRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> IngestSummary:
    require_own_profile(user, body.profile_id)
    settings = get_settings()
    if not settings.trakt_client_id:
        raise HTTPException(status_code=400, detail="TRAKT_CLIENT_ID must be configured to sync")
    _require_tmdb(settings)
    token = _resolve_token(session, body.profile_id, "trakt", body.access_token)
    since = _since_for(session, body.profile_id, "trakt", full=body.full)
    try:
        return _ingest_from(
            session,
            body.profile_id,
            _trakt_source(settings, token),
            source_name="trakt",
            since=since,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
        # Access token rejected on the first request (nothing ingested yet) — refresh + retry once.
        fresh = _refresh_trakt_token(session, settings, body.profile_id)
        return _ingest_from(
            session,
            body.profile_id,
            _trakt_source(settings, fresh),
            source_name="trakt",
            since=since,
        )


@router.post("/sources/trakt/connect/start", response_model=TraktConnectStartResponse)
def trakt_connect_start(
    session: Annotated[Session, Depends(get_session)],
) -> TraktConnectStartResponse:
    """Begin the Trakt OAuth device flow: returns the user code + verification URL to display."""
    code = _trakt_oauth(get_settings()).request_device_code()
    return TraktConnectStartResponse(
        device_code=code.device_code,
        user_code=code.user_code,
        verification_url=code.verification_url,
        interval=code.interval,
        expires_in=code.expires_in,
    )


@router.post("/sources/trakt/connect/poll", response_model=TraktConnectStatusResponse)
def trakt_connect_poll(
    body: TraktConnectPollRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> TraktConnectStatusResponse:
    """Poll once for authorization. On success, store the access token for this profile."""
    settings = get_settings()
    require_own_profile(user, body.profile_id)
    result = _trakt_oauth(settings).poll_token(body.device_code)
    if result.status is PollStatus.connected and result.access_token is not None:
        _store_trakt_tokens(
            session, settings, body.profile_id, result.access_token, result.refresh_token
        )
        session.commit()
    return TraktConnectStatusResponse(status=result.status.value)


@router.post("/sources/plex/sync", response_model=IngestSummary)
def sync_plex(
    body: PlexSyncRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> IngestSummary:
    """Sync the account owner's own Plex watch history (privacy-safe: single account)."""
    require_own_profile(user, body.profile_id)
    require_safe_url(body.base_url)
    _require_tmdb(get_settings())
    token = _resolve_token(session, body.profile_id, "plex", body.token)
    source = PlexSourceProvider(base_url=body.base_url, token=token, account_id=body.account_id)
    since = _since_for(session, body.profile_id, "plex", full=body.full)
    return _ingest_from(session, body.profile_id, source, source_name="plex", since=since)


@router.post("/sources/jellyfin/sync", response_model=IngestSummary)
def sync_jellyfin(
    body: JellyfinSyncRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> IngestSummary:
    """Sync one Jellyfin user's own played history (privacy-safe: single user)."""
    require_own_profile(user, body.profile_id)
    require_safe_url(body.base_url)
    _require_tmdb(get_settings())
    token = _resolve_token(session, body.profile_id, "jellyfin", body.api_key)
    source = JellyfinSourceProvider(base_url=body.base_url, api_key=token, user_id=body.user_id)
    since = _since_for(session, body.profile_id, "jellyfin", full=body.full)
    return _ingest_from(session, body.profile_id, source, source_name="jellyfin", since=since)


@router.get("/profiles/{profile_id}/sources", response_model=list[ConnectedSource])
def list_connected_sources(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> list[ConnectedSource]:
    """Which external services this profile has connected, and when each last synced."""
    require_own_profile(user, profile_id)
    rows = session.scalars(
        select(SourceToken).where(SourceToken.profile_id == profile_id).order_by(SourceToken.source)
    ).all()
    out: list[ConnectedSource] = []
    for row in rows:
        if row.source in _INTERNAL_SOURCES:
            continue
        kind = "requests" if row.source == "seerr" else "history"
        last = None if kind == "requests" else get_last_synced(session, profile_id, row.source)
        out.append(ConnectedSource(source=row.source, kind=kind, last_synced_at=last))
    return out
