"""Watch history endpoint (per-profile, paginated)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.api.schemas import HistoryItemResponse, HistoryPage
from phare.db.base import get_session
from phare.db.models import Episode, Season, Title, WatchEvent

router = APIRouter(tags=["History"])


@router.get("/history", response_model=HistoryPage)
def get_history(
    profile_id: Annotated[uuid.UUID, Query(alias="profileId")],
    session: Annotated[Session, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100, alias="perPage")] = 50,
) -> HistoryPage:
    """Return one profile's watch/rating history, most recent first.

    Per-profile isolation is enforced here: every query is scoped to ``profile_id``.
    """
    scoped = WatchEvent.profile_id == profile_id
    total = session.scalar(select(func.count()).select_from(WatchEvent).where(scoped))

    rows = session.execute(
        select(WatchEvent, Title, Season, Episode)
        .join(Title, WatchEvent.title_id == Title.id)
        .outerjoin(Season, WatchEvent.season_id == Season.id)
        .outerjoin(Episode, WatchEvent.episode_id == Episode.id)
        .where(scoped)
        .order_by(
            WatchEvent.occurred_at.desc().nulls_last(),
            WatchEvent.ingested_at.desc(),
        )
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()

    items = [
        HistoryItemResponse(
            id=event.id,
            title_id=title.id,
            title=title.title,
            kind=title.kind.value,
            type=event.type.value,
            rating=float(event.rating) if event.rating is not None else None,
            occurred_at=event.occurred_at,
            season_number=season.season_number if season is not None else None,
            episode_number=episode.episode_number if episode is not None else None,
            source=event.source,
            excluded=event.excluded,
        )
        for event, title, season, episode in rows
    ]
    return HistoryPage(items=items, page=page, per_page=per_page, total=total or 0)
