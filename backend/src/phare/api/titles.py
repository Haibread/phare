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
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.deps import get_language, get_optional_chat_llm
from phare.api.recommend import _poster_url, require_profile
from phare.api.schemas import TitleDetail
from phare.core.auth import get_current_user
from phare.core.config import get_settings
from phare.core.i18n import Language
from phare.db.base import get_session
from phare.db.models import TasteProfile, Title, TitleKind, User, WatchEvent
from phare.providers.tmdb import TMDBMetadataProvider
from phare.providers.types import LLMProvider
from phare.recommend.explain import (
    _EXPLANATION_CACHE,
    PersistentReasonCache,
    stream_lazy_reason,
)
from phare.recommend.schema import Anchor, Recommendation
from phare.taste.service import effective_profile

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Titles"])


def _localized_overview_genres(title: Title, language: Language) -> tuple[str | None, list[str]]:
    """The synopsis + genres in the request language.

    Stored catalog metadata is in whatever language it was imported in, so for the detail view —
    which the user explicitly opened — fetch the localised version live from TMDB when a key is
    configured. Falls back to the stored values on any miss, so the sheet never fails to render."""
    stored = (title.overview, list(title.genres))
    settings = get_settings()
    if not settings.tmdb_api_key or title.tmdb_id is None:
        return stored
    try:
        provider = TMDBMetadataProvider(
            api_key=settings.tmdb_api_key,
            base_url=settings.tmdb_base_url,
            language=language,
            cache_ttl=settings.tmdb_cache_ttl_seconds,
        )
        meta = provider.get_title(title.tmdb_id, title.kind)
    except Exception:  # noqa: BLE001 - a TMDB hiccup must not break the detail view
        logger.warning("titles.localize_failed", extra={"title_id": str(title.id)})
        return stored
    if meta is None:
        return stored
    return (meta.overview or title.overview, meta.genres or list(title.genres))


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
) -> TitleDetail:
    title = session.get(Title, title_id)
    if title is None:
        raise HTTPException(status_code=404, detail="Title not found")
    overview, genres = _localized_overview_genres(title, language)
    return TitleDetail(
        title_id=title.id,
        title=title.title,
        kind=title.kind.value,
        year=title.year,
        runtime_minutes=title.runtime_minutes,
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
