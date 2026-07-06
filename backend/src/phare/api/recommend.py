"""Recommendation rows endpoint + shared mappers/builders for the engine endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.api.deps import Embedder, get_embedder, get_language, get_optional_chat_llm
from phare.api.schemas import (
    ConversionResponse,
    RecommendationItem,
    RecommendationLogItem,
    RecommendationLogPage,
    RecommendationRow,
    RecommendationsResponse,
)
from phare.core.auth import get_current_user, require_own_profile
from phare.core.config import get_settings
from phare.core.i18n import DEFAULT_LANGUAGE, Language
from phare.db.base import get_session
from phare.db.models import Profile, RecommendationLog, User
from phare.eval.conversion import conversion_stats
from phare.providers.types import LLMProvider
from phare.recommend.dynamic import dynamic_rows
from phare.recommend.schema import Recommendation, Row
from phare.recommend.service import RecommendationService

router = APIRouter(tags=["Recommendations"])


def build_recommender(
    session: Session,
    embedder: Embedder,
    chat_llm: LLMProvider | None,
    language: Language = DEFAULT_LANGUAGE,
) -> RecommendationService:
    """Construct the engine from request-scoped dependencies + tuning config."""
    settings = get_settings()
    return RecommendationService(
        session,
        embed_provider=embedder.provider,
        embed_read_version=embedder.read_version,
        embed_write_version=embedder.write_version,
        chat_llm=chat_llm,
        row_size=settings.recommend_row_size,
        swing_slots=settings.recommend_swing_slots,
        explanation_budget=settings.recommend_explanation_budget,
        language=language,
    )


def _poster_url(poster_path: str | None) -> str | None:
    """Compose a full poster URL from a TMDB ``poster_path``; the frontend stays dumb."""
    if not poster_path:
        return None
    return f"{get_settings().tmdb_image_base_url}{poster_path}"


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
        poster_url=_poster_url(rec.poster_path),
        components=rec.components,
        watched=rec.watched,
    )


def to_row(row: Row) -> RecommendationRow:
    return RecommendationRow(key=row.key, title=row.title, items=[to_item(i) for i in row.items])


def require_profile(user: User, profile_id: uuid.UUID) -> Profile:
    """Enforce isolation and return the caller's profile. 1:1, so it's the user's own profile."""
    require_own_profile(user, profile_id)
    assert user.profile is not None  # require_own_profile guarantees it
    return user.profile


@router.get("/profiles/{profile_id}/recommendations", response_model=RecommendationsResponse)
def get_recommendations(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
    language: Annotated[Language, Depends(get_language)],
) -> RecommendationsResponse:
    require_profile(user, profile_id)
    recommender = build_recommender(session, embedder, chat_llm, language)
    rows = recommender.rows(profile_id)
    session.commit()  # lazy embeddings (and any logging) persist
    return RecommendationsResponse(
        rows=[to_row(row) for row in rows],
        embeddings_degraded=embedder.degraded,
        profile_building=recommender.profile_building(profile_id),
    )


@router.get(
    "/profiles/{profile_id}/recommendations/dynamic", response_model=RecommendationsResponse
)
def get_dynamic_recommendations(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
    language: Annotated[Language, Depends(get_language)],
) -> RecommendationsResponse:
    """Today's LLM-picked themed rows (calendar + taste fallback when no LLM is configured)."""
    require_profile(user, profile_id)
    recommender = build_recommender(session, embedder, chat_llm, language)
    rows, degraded = dynamic_rows(recommender, profile_id, llm=chat_llm)
    session.commit()
    return RecommendationsResponse(rows=[to_row(row) for row in rows], degraded=degraded)


@router.get("/profiles/{profile_id}/recommendations/conversion", response_model=ConversionResponse)
def get_conversion(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    top_k: Annotated[int, Query(ge=1, le=100, alias="topK")] = 10,
    within_days: Annotated[int, Query(ge=1, le=365, alias="withinDays")] = 14,
) -> ConversionResponse:
    """Closed-loop north star: of titles shown in the top-K, the fraction watched within N days."""
    require_profile(user, profile_id)
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
    user: Annotated[User, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100, alias="perPage")] = 50,
) -> RecommendationLogPage:
    """Inspect what was recommended to a profile (and when). Recs are never a black box."""
    require_profile(user, profile_id)
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
