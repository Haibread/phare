"""Source sync + connect endpoints (Trakt OAuth device flow, Plex, Jellyfin)."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.deps import get_language, require_safe_url
from phare.api.schemas import (
    ApiModel,
    IngestSummary,
    TraktConnectPollRequest,
    TraktConnectStartResponse,
    TraktConnectStatusResponse,
)
from phare.core.auth import get_current_user, require_own_profile
from phare.core.config import Settings, get_settings
from phare.core.fallback import record_fallback
from phare.core.i18n import DEFAULT_LANGUAGE, Language
from phare.core.sync_state import get_last_synced, set_last_synced
from phare.core.tokens import get_source_token, store_source_token
from phare.db.base import get_session
from phare.db.models import Profile, SourceToken, Title, TitleKind, User
from phare.ingest.service import IngestionService, IngestResult
from phare.providers.jellyfin import JellyfinSourceProvider, list_jellyfin_users
from phare.providers.plex import PlexSourceProvider
from phare.providers.tmdb import TMDBMetadataProvider
from phare.providers.trakt import TraktSourceProvider
from phare.providers.trakt_oauth import PollStatus, TraktOAuth
from phare.providers.types import MetadataProvider, RawEvent, RawMediaType, SourceProvider
from phare.taste.service import maybe_refresh_taste, optional_llm_provider

router = APIRouter(tags=["Sync"])
logger = logging.getLogger(__name__)

# Internal token rows that aren't user-facing "connected sources".
_INTERNAL_SOURCES = {"trakt_refresh"}

# How long a graceful shutdown waits for an in-flight auto-sync pass (the thread is a daemon, so it
# dies with the process regardless — this just lets a quick pass finish cleanly).
_AUTO_SYNC_JOIN_TIMEOUT = 5.0


class PartialSyncError(Exception):
    """A source sync failed partway through, after at least one batch was already committed.

    The committed batches stay in the DB (the upsert is idempotent), so re-running the sync resumes
    where it stopped without duplicates. Carries how many watch events durably landed so the API can
    tell the user what's safe instead of a bare 500 (review G3). The originating error is chained as
    ``__cause__`` for the logs.
    """

    def __init__(self, ingested: int, cause: BaseException) -> None:
        super().__init__(f"sync failed after {ingested} events ingested")
        self.ingested = ingested
        self.__cause__ = cause


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
    # Keyed by (tmdb_id, kind): a movie and a show can share a tmdb_id, and they're distinct titles
    # to resolve (review H3a).
    wanted: set[tuple[int, TitleKind]] = set()
    for event in batch:
        if event.tmdb_id is not None:
            kind = TitleKind.movie if event.media_type is RawMediaType.movie else TitleKind.show
            wanted.add((event.tmdb_id, kind))
    if not wanted:
        return
    # Skip titles already in the catalog — the ingest resolves those from the DB, no HTTP needed.
    tmdb_ids = {tmdb_id for tmdb_id, _ in wanted}
    stored = {
        (row.tmdb_id, row.kind)
        for row in session.execute(
            select(Title.tmdb_id, Title.kind).where(Title.tmdb_id.in_(tmdb_ids))
        )
    }
    missing = [pair for pair in wanted if pair not in stored]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=_PREWARM_WORKERS) as pool:
        # Results land in the provider's shared cache; the sequential ingest below hits it.
        list(pool.map(lambda pair: metadata.get_title(pair[0], pair[1]), missing))


class ConnectedSource(ApiModel):
    source: str
    kind: str  # "history" | "requests"
    last_synced_at: datetime | None = None


class SourceCapabilities(ApiModel):
    """Which sources this server is configured to support, so the UI can disable the rest instead
    of surfacing a raw config error when a user clicks (review D1). Every history source resolves
    titles through TMDB, so all three need ``TMDB_API_KEY``; Trakt additionally needs its
    server-side OAuth app credentials. Seerr only stores user-supplied credentials at connect time,
    so it's always offered."""

    trakt: bool
    plex: bool
    jellyfin: bool
    seerr: bool
    # Whether the sample-data escape hatch is available. The sample endpoints (POST /catalog/sample,
    # POST /profiles/{id}/sample-data) are disabled in production, so the UI must hide the button
    # rather than offer a trap that 403s. Mirrors the same gate those endpoints raise on.
    sample_data: bool


@router.get("/sources/capabilities", response_model=SourceCapabilities)
def source_capabilities(
    user: Annotated[User, Depends(get_current_user)],
) -> SourceCapabilities:
    """Report which sources the operator has configured on this server (review D1)."""
    settings = get_settings()
    tmdb = bool(settings.tmdb_api_key)
    return SourceCapabilities(
        trakt=tmdb and bool(settings.trakt_client_id) and bool(settings.trakt_client_secret),
        plex=tmdb,
        jellyfin=tmdb,
        seerr=True,
        # Same condition the sample endpoints gate on (disabled in production).
        sample_data=not settings.is_production,
    )


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
    # Watch events durably committed so far — reported if a later batch (or the upstream pull) fails
    # mid-stream, so the user learns how much already landed (review G3).
    committed = 0

    def flush() -> None:
        nonlocal committed
        if not batch:
            return
        _prewarm_metadata(session, metadata, batch)
        part = ingester.ingest(profile_id, batch)
        totals.created += part.created
        totals.updated += part.updated
        totals.skipped += part.skipped
        totals.titles_created += part.titles_created
        session.commit()
        committed += part.created + part.updated
        batch.clear()

    try:
        for event in events:
            batch.append(event)
            if len(batch) >= batch_size:
                flush()
        flush()
    except Exception as exc:
        # Nothing committed yet → let the original error surface unchanged (e.g. a first-request 401
        # the Trakt handler recovers from by refreshing the token). Once a batch is durable, report
        # the partial state so the UI can say "N imported, re-run to resume" instead of a bare 500.
        if committed > 0:
            session.rollback()  # drop the uncommitted in-flight batch; committed ones are untouched
            raise PartialSyncError(committed, exc) from exc
        raise
    return totals


def _ingest_from(
    session: Session,
    profile_id: uuid.UUID,
    source: SourceProvider,
    *,
    source_name: str,
    since: datetime | None,
    language: Language = DEFAULT_LANGUAGE,
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
        maybe_refresh_taste(session, profile_id, optional_llm_provider(), language)
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


class JellyfinUsersRequest(ApiModel):
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)


class JellyfinUser(ApiModel):
    id: str
    name: str


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


def _trakt_ingest_with_refresh(
    session: Session,
    settings: Settings,
    profile_id: uuid.UUID,
    token: str,
    *,
    since: datetime | None,
    language: Language,
) -> IngestSummary:
    """Ingest a profile's Trakt history, refreshing an expired access token once on a first-request
    401. Shared by the sync endpoint and the background auto-sync so both recover from an expired
    token the same way. A 401 mid-stream (after a batch committed) surfaces as ``PartialSyncError``
    from ``_ingest_from`` — not retried here; the committed batches let the next run resume."""
    try:
        return _ingest_from(
            session,
            profile_id,
            _trakt_source(settings, token),
            source_name="trakt",
            since=since,
            language=language,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
        # Access token rejected on the first request (nothing ingested yet) — refresh + retry once.
        fresh = _refresh_trakt_token(session, settings, profile_id)
        return _ingest_from(
            session,
            profile_id,
            _trakt_source(settings, fresh),
            source_name="trakt",
            since=since,
            language=language,
        )


@router.post("/sources/trakt/sync", response_model=IngestSummary)
def sync_trakt(
    body: TraktSyncRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    language: Annotated[Language, Depends(get_language)],
) -> IngestSummary:
    require_own_profile(user, body.profile_id)
    settings = get_settings()
    if not settings.trakt_client_id:
        raise HTTPException(status_code=400, detail="TRAKT_CLIENT_ID must be configured to sync")
    _require_tmdb(settings)
    token = _resolve_token(session, body.profile_id, "trakt", body.access_token)
    since = _since_for(session, body.profile_id, "trakt", full=body.full)
    return _trakt_ingest_with_refresh(
        session, settings, body.profile_id, token, since=since, language=language
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
    language: Annotated[Language, Depends(get_language)],
) -> IngestSummary:
    """Sync the account owner's own Plex watch history (privacy-safe: single account)."""
    require_own_profile(user, body.profile_id)
    require_safe_url(body.base_url)
    _require_tmdb(get_settings())
    token = _resolve_token(session, body.profile_id, "plex", body.token)
    source = PlexSourceProvider(base_url=body.base_url, token=token, account_id=body.account_id)
    since = _since_for(session, body.profile_id, "plex", full=body.full)
    return _ingest_from(
        session, body.profile_id, source, source_name="plex", since=since, language=language
    )


@router.post("/sources/jellyfin/sync", response_model=IngestSummary)
def sync_jellyfin(
    body: JellyfinSyncRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    language: Annotated[Language, Depends(get_language)],
) -> IngestSummary:
    """Sync one Jellyfin user's own played history (privacy-safe: single user)."""
    require_own_profile(user, body.profile_id)
    require_safe_url(body.base_url)
    _require_tmdb(get_settings())
    token = _resolve_token(session, body.profile_id, "jellyfin", body.api_key)
    source = JellyfinSourceProvider(base_url=body.base_url, api_key=token, user_id=body.user_id)
    since = _since_for(session, body.profile_id, "jellyfin", full=body.full)
    return _ingest_from(
        session, body.profile_id, source, source_name="jellyfin", since=since, language=language
    )


@router.post("/sources/jellyfin/users", response_model=list[JellyfinUser])
def jellyfin_users(
    body: JellyfinUsersRequest,
    user: Annotated[User, Depends(get_current_user)],
) -> list[JellyfinUser]:
    """List a Jellyfin server's users (URL + API key) so the UI offers a picker, not a raw GUID
    field (review D2). The SSRF guard applies — internal URLs are rejected."""
    require_safe_url(body.base_url)
    users = list_jellyfin_users(body.base_url, body.api_key)
    return [JellyfinUser(id=item["id"], name=item["name"]) for item in users]


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


# --- Background auto-sync -------------------------------------------------------------------------
#
# Sources are synced on a manual button today; if you watch things all week your Phare taste drifts
# until the next click. Principle 8 (self-triggering over manual steps): a background pass keeps
# each connected account current on its own. Mirrors the catalog-refresh loop (catalog/refresh.py):
# a single daemon thread, an interruptible wait, work split from wiring so tests drive it on a
# rolled-back session. **Trakt only** — Plex/Jellyfin need their server URL (and Jellyfin user id),
# which are supplied per request and never persisted, so they can't be reconstructed unattended.


def _auto_sync_configured(settings: Settings) -> bool:
    """Whether unattended Trakt sync can run: its OAuth app credentials + TMDB are both present."""
    return bool(settings.trakt_client_id) and bool(settings.tmdb_api_key)


def _profiles_with_trakt_token(session: Session) -> list[uuid.UUID]:
    """Every profile that has a stored Trakt access token — the auto-sync candidates."""
    return list(
        session.scalars(
            select(SourceToken.profile_id)
            .where(SourceToken.source == "trakt")
            .order_by(SourceToken.profile_id)
        )
    )


def _auto_sync_trakt_profile(
    session: Session, settings: Settings, profile_id: uuid.UUID, language: Language
) -> None:
    """Incrementally sync one profile's Trakt history from its stored token. Skips silently when the
    token has gone (a concurrent disconnect); the caller isolates any other failure."""
    token = get_source_token(session, settings, profile_id, "trakt")
    if token is None:
        return
    since = get_last_synced(session, profile_id, "trakt")
    _trakt_ingest_with_refresh(session, settings, profile_id, token, since=since, language=language)


def run_auto_sync(
    session: Session,
    settings: Settings,
    *,
    sync_profile: Callable[[Session, Settings, uuid.UUID, Language], None] | None = None,
    language: Language = DEFAULT_LANGUAGE,
) -> int:
    """Incrementally sync every profile with a stored Trakt token. Returns the count synced.

    Best-effort per profile: a failure (expired-and-unrefreshable token, provider outage, a
    ``PartialSyncError`` mid-stream) is rolled back, recorded on the fallback metric, logged, and
    the next profile continues — one bad account never stalls the rest or the loop. The taste
    refresh inside the sync regenerates in the default language, but the profile still *displays* in
    the UI language (``summary_by_lang`` translates on demand — F1), so an unattended pass doesn't
    pin a user's summary to English. Split from the thread wiring so tests drive it synchronously
    with a fake ``sync_profile``."""
    worker = sync_profile or _auto_sync_trakt_profile
    synced = 0
    for profile_id in _profiles_with_trakt_token(session):
        try:
            worker(session, settings, profile_id, language)
            synced += 1
        except Exception:  # noqa: BLE001 - one profile's failure must not stop the others
            session.rollback()
            record_fallback("auto_sync", "profile_failed")
            logger.exception("auto_sync.profile_failed", extra={"profile_id": str(profile_id)})
    return synced


def run_auto_sync_once(settings: Settings) -> int:
    """One auto-sync pass on its own session. Returns profiles synced (0 when unconfigured).
    Best-effort: swallows everything so a flaky network never kills the loop (or the first tick)."""
    if not _auto_sync_configured(settings):
        return 0
    from phare.db.base import get_session_factory

    try:
        with get_session_factory()() as session:
            return run_auto_sync(session, settings)
    except Exception:  # noqa: BLE001 - an auto-sync pass must never crash the loop / process
        logger.exception("auto_sync.failed")
        return 0


def start_auto_sync_loop(settings: Settings) -> Callable[[], None]:
    """Start the recurring source auto-sync in a daemon thread; return a ``stop()`` that ends it.

    No-op (returns a no-op stop) when disabled (``source_sync_interval_seconds <= 0``) or when Trakt
    isn't configured. The first pass fires after the short ``source_sync_initial_delay_seconds``
    (not a full interval), so a box that restarts more often than the interval still syncs.
    ``stop()`` signals the loop and waits briefly for an in-flight pass. Mirrors
    ``start_refresh_loop``."""
    interval = settings.source_sync_interval_seconds
    if interval <= 0 or not _auto_sync_configured(settings):
        return lambda: None

    stop = threading.Event()
    initial_delay = max(0, settings.source_sync_initial_delay_seconds)

    def loop() -> None:
        # Event.wait returns True only when stop is set; on timeout it returns False → run a pass.
        if stop.wait(initial_delay):
            return
        run_auto_sync_once(settings)
        while not stop.wait(interval):
            run_auto_sync_once(settings)

    thread = threading.Thread(target=loop, name="source-auto-sync", daemon=True)
    thread.start()
    logger.info("auto_sync.scheduled", extra={"interval_seconds": interval})

    def shutdown() -> None:
        stop.set()
        thread.join(timeout=_AUTO_SYNC_JOIN_TIMEOUT)

    return shutdown
