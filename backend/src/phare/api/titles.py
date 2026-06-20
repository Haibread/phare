"""Title detail — the "more info" view behind a recommendation card.

Profile-agnostic metadata (synopsis, runtime, external links); per-profile availability/requests
live in ``actions``. The synopsis is the title's own ``overview`` (the official TMDB blurb) — this
is the one place we surface it, since the user explicitly opened it; recommendation *explanations*
still never include plot (see ``recommend/explain.py``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.deps import get_optional_chat_llm
from phare.api.recommend import _poster_url, require_profile
from phare.api.schemas import TitleDetail
from phare.db.base import get_session
from phare.db.models import TasteProfile, Title, TitleKind
from phare.providers.types import LLMProvider
from phare.recommend.explain import _EXPLANATION_CACHE, stream_lazy_reason
from phare.recommend.schema import Recommendation
from phare.taste.service import effective_profile

router = APIRouter(tags=["Titles"])


def _tmdb_url(title: Title) -> str | None:
    if title.tmdb_id is None:
        return None
    kind = "movie" if title.kind is TitleKind.movie else "tv"
    return f"https://www.themoviedb.org/{kind}/{title.tmdb_id}"


@router.get("/titles/{title_id}", response_model=TitleDetail)
def get_title(
    title_id: uuid.UUID, session: Annotated[Session, Depends(get_session)]
) -> TitleDetail:
    title = session.get(Title, title_id)
    if title is None:
        raise HTTPException(status_code=404, detail="Title not found")
    return TitleDetail(
        title_id=title.id,
        title=title.title,
        kind=title.kind.value,
        year=title.year,
        runtime_minutes=title.runtime_minutes,
        genres=title.genres,
        overview=title.overview,
        poster_url=_poster_url(title.poster_path),
        tmdb_url=_tmdb_url(title),
        imdb_url=f"https://www.imdb.com/title/{title.imdb_id}/" if title.imdb_id else None,
    )


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/profiles/{profile_id}/titles/{title_id}/explanation")
def stream_title_explanation(
    profile_id: uuid.UUID,
    title_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
) -> StreamingResponse:
    """The LLM "why this fits you" reason, generated **lazily and streamed** — only when the user
    opens a card's detail sheet, so we never pay to explain cards nobody opens. Server-Sent Events:
    ``delta`` chunks as the (workhorse) model types, then ``done``. Cached per (title, taste) so a
    re-open returns instantly as one chunk; offline streams the deterministic template."""
    require_profile(session, profile_id)
    title = session.get(Title, title_id)
    if title is None:
        raise HTTPException(status_code=404, detail="Title not found")
    taste_row = session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))
    taste = effective_profile(taste_row) if taste_row is not None else {}
    rec = Recommendation(
        title_id=title.id,
        title=title.title,
        kind=title.kind.value,
        year=title.year,
        genres=title.genres,
        score=0.0,
    )

    def events() -> Iterator[str]:
        for chunk in stream_lazy_reason(rec, taste, chat_llm, _EXPLANATION_CACHE):
            yield _sse("delta", {"text": chunk})
        yield _sse("done", {})

    return StreamingResponse(events(), media_type="text/event-stream")
