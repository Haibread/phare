"""Title detail — the "more info" view behind a recommendation card.

Profile-agnostic metadata (synopsis, runtime, external links); per-profile availability/requests
live in ``actions``. The synopsis is the title's own ``overview`` (the official TMDB blurb) — this
is the one place we surface it, since the user explicitly opened it; recommendation *explanations*
still never include plot (see ``recommend/explain.py``).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from phare.api.deps import (
    get_language,
    get_optional_chat_llm,
    get_optional_metadata_provider,
)
from phare.api.recommend import _poster_url, require_profile
from phare.api.schemas import TitleDetail
from phare.core.auth import get_current_user
from phare.core.config import get_settings
from phare.core.fallback import record_fallback
from phare.core.i18n import Language
from phare.db.base import get_session
from phare.db.models import (
    TasteProfile,
    Title,
    TitleKind,
    TitleLocalization,
    User,
    WatchEvent,
)
from phare.providers.types import LLMProvider, MetadataProvider
from phare.recommend.explain import (
    _EXPLANATION_CACHE,
    PersistentReasonCache,
    stream_lazy_reason,
)
from phare.recommend.schema import Anchor, Recommendation
from phare.taste.service import effective_profile

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Titles"])


def _localized_overview_genres(
    session: Session,
    title: Title,
    language: Language,
    provider: MetadataProvider | None,
) -> tuple[str | None, list[str], bool]:
    """The synopsis + genres in the request language, cache-first.

    The third return value is whether this call actually fetched from the provider — the runtime
    backfill uses it to avoid a redundant second TMDB round-trip on the same request.

    Stored catalog metadata is in whatever language it was imported in, so the detail view wants the
    localized version. Fetching it from TMDB live on every open cost ~6 s (review C2), so it's
    cached per (title, language) in ``title_localization`` with a long TTL. A copy inside the TTL
    is served without touching TMDB; past the TTL (or on a first open) it's re-fetched and the cache
    refreshed. When TMDB is unreachable — or unconfigured — any stored copy (even stale) is served
    over the wrong-language base metadata, so the sheet always renders.
    """
    stored = (title.overview, list(title.genres))
    cached = session.get(TitleLocalization, {"title_id": title.id, "language": language})

    def _from_cache() -> tuple[str | None, list[str]]:
        # A cached field may be empty (TMDB had none) — fall back to the base metadata there.
        return (cached.overview or title.overview, list(cached.genres) or list(title.genres))

    now = datetime.now(UTC)
    ttl = timedelta(seconds=get_settings().title_localization_ttl_seconds)
    if cached is not None and now - cached.fetched_at < ttl:
        return (*_from_cache(), False)
    if provider is None or title.tmdb_id is None:
        # No way to refresh — serve any stored copy (even stale) over the wrong-language base text.
        return (*(_from_cache() if cached is not None else stored), False)
    try:
        meta = provider.get_title(title.tmdb_id, title.kind)
    except Exception:  # noqa: BLE001 - a TMDB hiccup must not break the detail view
        logger.warning("titles.localize_failed", extra={"title_id": str(title.id)})
        record_fallback("titles", "localization_unavailable", title_id=str(title.id))
        # ``False``: no metadata was actually obtained, so the runtime backfill on this request
        # still gets its one best-effort attempt — the flag means "already *successfully*
        # consulted", not "already tried". (The failed localization isn't cached, so a later
        # open retries either way; review finding round-4.)
        return (*(_from_cache() if cached is not None else stored), False)
    if meta is None:
        return (*(_from_cache() if cached is not None else stored), True)
    # We already have the full metadata in hand — opportunistically heal a missing runtime from the
    # same fetch (see `_backfill_runtime`), so a detail open never costs a second TMDB round-trip.
    if title.runtime_minutes is None and meta.runtime_minutes is not None:
        title.runtime_minutes = meta.runtime_minutes
    _upsert_localization(session, title.id, language, meta.overview, list(meta.genres), now)
    return (meta.overview or title.overview, list(meta.genres) or list(title.genres), True)


def _upsert_localization(
    session: Session,
    title_id: uuid.UUID,
    language: Language,
    overview: str | None,
    genres: list[str],
    when: datetime,
) -> None:
    """Cache (or refresh) a title's localized synopsis + genres. The caller owns the commit."""
    values = {"overview": overview, "genres": genres, "fetched_at": when}
    session.execute(
        pg_insert(TitleLocalization)
        .values(title_id=title_id, language=language, **values)
        .on_conflict_do_update(index_elements=["title_id", "language"], set_=values)
    )


def _backfill_runtime(
    title: Title, provider: MetadataProvider | None, *, localized_fetched: bool
) -> int | None:
    """Fill a title's missing runtime from the metadata provider, lazily on the detail read.

    *discover* (the broad catalog import) omits per-title runtime, so a mainstream title like
    Inception can sit at ``runtime_minutes = NULL`` — and its detail sheet then shows no runtime.
    The recommend path only heals runtimes for turns that ask for a length cap (see
    ``recommend/service._enrich_runtimes``), which never covers a plain detail open. So the detail
    read backfills it here, persisted permanently, so the catalog heals as titles are opened — no
    command, no manual step.

    Localization on the same request already fills the runtime from *its* TMDB fetch when it fetches
    (so the common first-open path spends a single round-trip). This is the fallback for the case it
    can't cover — a warm localization cache (or empty synopsis) that still left the runtime NULL —
    where a dedicated fetch is the only way to heal it.

    Best-effort: a TMDB hiccup (or no key / no ``tmdb_id``) leaves the runtime NULL and the detail
    still renders. The caller owns the commit.
    """
    if title.runtime_minutes is not None:
        return title.runtime_minutes
    # Localization already hit the provider this request and found no runtime — a second fetch would
    # return the same nothing. Only fetch here when localization served from cache (or offline).
    if localized_fetched or provider is None or title.tmdb_id is None:
        return None
    try:
        meta = provider.get_title(title.tmdb_id, title.kind)
    except Exception:  # noqa: BLE001 - a TMDB hiccup must not break the detail view
        logger.warning("titles.runtime_backfill_failed", extra={"title_id": str(title.id)})
        record_fallback("titles", "runtime_unavailable", title_id=str(title.id))
        return None
    if meta is None or meta.runtime_minutes is None:
        return None
    title.runtime_minutes = meta.runtime_minutes
    logger.info("titles.runtime_backfilled", extra={"title_id": str(title.id)})
    return meta.runtime_minutes


def _tmdb_url(title: Title) -> str | None:
    if title.tmdb_id is None:
        return None
    kind = "movie" if title.kind is TitleKind.movie else "tv"
    return f"https://www.themoviedb.org/{kind}/{title.tmdb_id}"


@router.get("/titles/{title_id}", response_model=TitleDetail)
def get_title(
    title_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    language: Annotated[Language, Depends(get_language)],
    provider: Annotated[MetadataProvider | None, Depends(get_optional_metadata_provider)],
) -> TitleDetail:
    title = session.get(Title, title_id)
    if title is None:
        raise HTTPException(status_code=404, detail="Title not found")
    overview, genres, localized_fetched = _localized_overview_genres(
        session, title, language, provider
    )
    runtime_minutes = _backfill_runtime(title, provider, localized_fetched=localized_fetched)
    session.commit()  # persist the localization cache fill and any runtime backfill
    return TitleDetail(
        title_id=title.id,
        title=title.title,
        kind=title.kind.value,
        year=title.year,
        runtime_minutes=runtime_minutes,
        genres=genres,
        overview=overview,
        poster_url=_poster_url(title.poster_path),
        tmdb_url=_tmdb_url(title),
        imdb_url=f"https://www.imdb.com/title/{title.imdb_id}/" if title.imdb_id else None,
    )


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _load_anchor(
    session: Session, profile_id: uuid.UUID, because: uuid.UUID | None
) -> Anchor | None:
    """The "because you watched X" seed, honoured **only** if the viewer actually has history with
    it — which it always does for a real "because" row, since those are built from their own loved
    titles. The history check keeps the param from being used to probe arbitrary title-to-title
    links. Returns ``None`` (taste-only explanation) for an absent / unknown / unwatched anchor."""
    if because is None:
        return None
    watched = session.scalar(
        select(WatchEvent.id)
        .where(WatchEvent.profile_id == profile_id, WatchEvent.title_id == because)
        .limit(1)
    )
    if watched is None:
        return None
    seed = session.get(Title, because)
    if seed is None:
        return None
    return Anchor(title_id=seed.id, title=seed.title, genres=list(seed.genres))


@router.get("/profiles/{profile_id}/titles/{title_id}/explanation")
def stream_title_explanation(
    profile_id: uuid.UUID,
    title_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
    language: Annotated[Language, Depends(get_language)],
    because: uuid.UUID | None = None,
) -> StreamingResponse:
    """The LLM "why this fits you" reason, generated **lazily and streamed** — only when the user
    opens a card's detail sheet, so we never pay to explain cards nobody opens. Server-Sent Events:
    ``delta`` chunks as the (workhorse) model types, then ``done``. Cached per (title, taste,
    anchor) so a re-open returns instantly as one chunk; offline streams the deterministic template.

    ``because`` is the seed title of a "because you watched X" row (optional): when present and the
    viewer has watched it, the reason opens from that concrete link, not the abstract taste."""
    require_profile(user, profile_id)
    title = session.get(Title, title_id)
    if title is None:
        raise HTTPException(status_code=404, detail="Title not found")
    taste_row = session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))
    taste = effective_profile(taste_row) if taste_row is not None else {}
    anchor = _load_anchor(session, profile_id, because)
    rec = Recommendation(
        title_id=title.id,
        title=title.title,
        kind=title.kind.value,
        year=title.year,
        genres=title.genres,
        score=0.0,
        overview=title.overview,
        keywords=list(title.keywords),
    )

    # Two-tier cache: in-process L1 over a Postgres L2, so an accepted reason survives restarts
    # instead of being re-generated (a workhorse call) after every recycle.
    cache = PersistentReasonCache(session, _EXPLANATION_CACHE)

    def events() -> Iterator[str]:
        for chunk in stream_lazy_reason(rec, taste, chat_llm, cache, language, anchor):
            yield _sse("delta", {"text": chunk})
        yield _sse("done", {})

    return StreamingResponse(events(), media_type="text/event-stream")
