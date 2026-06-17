"""Recommendation rows endpoint + shared mappers/builders for the engine endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.api.deps import Embedder, get_embedder, get_optional_chat_llm
from phare.api.schemas import (
    ConversionResponse,
    RecommendationItem,
    RecommendationLogItem,
    RecommendationLogPage,
    RecommendationRow,
    RecommendationsResponse,
)
from phare.core.config import get_settings
from phare.db.base import get_session
from phare.db.models import Profile, RecommendationLog
from phare.eval.conversion import conversion_stats
from phare.providers.types import LLMProvider
from phare.recommend.schema import Recommendation, Row
from phare.recommend.service import RecommendationService

router = APIRouter(tags=["Recommendations"])


def build_recommender(
    session: Session,
    embedder: Embedder,
    chat_llm: LLMProvider | None,
) -> RecommendationService:
    """Construct the engine from request-scoped dependencies + tuning config."""
    settings = get_settings()
    return RecommendationService(
        session,
        embed_provider=embedder.provider,
        embed_model_version=embedder.model_version,
        chat_llm=chat_llm,
        row_size=settings.recommend_row_size,
        swing_slots=settings.recommend_swing_slots,
    )


def to_item(rec: Recommendation) -> RecommendationItem:
    return RecommendationItem(
        title_id=rec.title_id,
        title=rec.title,
        kind=rec.kind,
        year=rec.year,
        genres=rec.genres,
        score=rec.score,
        is_swing=rec.is_swing,
        confidence=rec.confidence,
        explanation=rec.explanation,
        components=rec.components,
    )


def to_row(row: Row) -> RecommendationRow:
    return RecommendationRow(key=row.key, title=row.title, items=[to_item(i) for i in row.items])


def require_profile(session: Session, profile_id: uuid.UUID) -> Profile:
    profile = session.get(Profile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.get("/profiles/{profile_id}/recommendations", response_model=RecommendationsResponse)
def get_recommendations(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
) -> RecommendationsResponse:
    require_profile(session, profile_id)
    recommender = build_recommender(session, embedder, chat_llm)
    rows = recommender.rows(profile_id)
    session.commit()  # lazy embeddings (and any logging) persist
    return RecommendationsResponse(rows=[to_row(row) for row in rows])


@router.get("/profiles/{profile_id}/recommendations/conversion", response_model=ConversionResponse)
def get_conversion(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    top_k: Annotated[int, Query(ge=1, le=100, alias="topK")] = 10,
    within_days: Annotated[int, Query(ge=1, le=365, alias="withinDays")] = 14,
) -> ConversionResponse:
    """Closed-loop north star: of titles shown in the top-K, the fraction watched within N days."""
    require_profile(session, profile_id)
    stats = conversion_stats(session, profile_id=profile_id, top_k=top_k, within_days=within_days)
    return ConversionResponse(
        shown=stats.shown,
        converted=stats.converted,
        rate=stats.rate,
        swing_shown=stats.swing_shown,
        swing_converted=stats.swing_converted,
        swing_rate=stats.swing_rate,
        top_k=stats.top_k,
        within_days=stats.within_days,
    )


@router.get("/profiles/{profile_id}/recommendations/log", response_model=RecommendationLogPage)
def get_recommendation_log(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100, alias="perPage")] = 50,
) -> RecommendationLogPage:
    """Inspect what was recommended to a profile (and when). Recs are never a black box."""
    require_profile(session, profile_id)
    where = RecommendationLog.profile_id == profile_id
    total = session.scalar(select(func.count()).select_from(RecommendationLog).where(where)) or 0
    rows = session.scalars(
        select(RecommendationLog)
        .where(where)
        .order_by(RecommendationLog.shown_at.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()
    return RecommendationLogPage(
        items=[
            RecommendationLogItem(
                id=row.id,
                title_id=row.title_id,
                row_key=row.row_key,
                rank=row.rank,
                score=row.score,
                is_swing=row.is_swing,
                source=row.source,
                shown_at=row.shown_at,
            )
            for row in rows
        ],
        page=page,
        per_page=per_page,
        total=total,
    )
