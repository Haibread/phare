"""Taste profile: view, generate (LLM), and edit (sticky user overrides) — plus the read-only
facet view (the inspectable taste modes, principle 2)."""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.deps import Embedder, get_embedder, get_language
from phare.api.recommend import _poster_url
from phare.api.schemas import (
    FacetExemplarResponse,
    LLMUnavailable,
    TasteFacetResponse,
    TasteFacetsResponse,
    TasteResponse,
    UpdateTasteRequest,
)
from phare.core.auth import get_current_user, require_own_profile
from phare.core.config import get_settings
from phare.core.fallback import record_fallback
from phare.core.i18n import Language
from phare.core.llm_budget import LLMBudgetExceeded
from phare.db.base import get_session
from phare.db.models import TasteProfile, Title, User
from phare.providers.llm import OpenAILLMProvider
from phare.providers.types import LLMProvider
from phare.recommend.taste_facets import extract_facets, rank_members_by_centrality
from phare.recommend.taste_vector import taste_contributions
from phare.taste.service import (
    TasteService,
    effective_profile,
    localized_display,
    optional_llm_provider,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Taste"])


def get_llm_provider() -> LLMProvider:
    """Build the configured LLM provider, or 400 if it isn't set up."""
    settings = get_settings()
    if not settings.llm_api_key:
        raise HTTPException(status_code=400, detail="LLM is not configured (set LLM_API_KEY)")
    return OpenAILLMProvider(
        api_key=settings.llm_api_key,
        chat_model=settings.llm_chat_model,
        embedding_model=settings.llm_embedding_model,
        base_url=settings.llm_base_url,
        monthly_token_budget=settings.llm_monthly_token_budget,
        reasoning_headroom=settings.reasoning_headroom,
        timeout=settings.llm_timeout_seconds,
    )


def _to_response(
    taste: TasteProfile,
    summary: str | None = None,
    display_terms: dict[str, str] | None = None,
) -> TasteResponse:
    return TasteResponse(
        profile_id=taste.profile_id,
        summary=summary if summary is not None else taste.summary_text,
        structured=effective_profile(taste),
        display_terms=display_terms or {},
        user_overrides=taste.user_overrides,
        confidence=taste.confidence,
        model_version=taste.model_version,
        generated_at=taste.generated_at,
    )


def _require_taste(session: Session, profile_id: uuid.UUID) -> TasteProfile:
    taste = session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))
    if taste is None:
        raise HTTPException(status_code=404, detail="No taste profile yet; generate one first")
    return taste


@router.get("/profiles/{profile_id}/taste", response_model=TasteResponse)
def get_taste(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    language: Annotated[Language, Depends(get_language)],
) -> TasteResponse:
    require_own_profile(user, profile_id)
    taste = _require_taste(session, profile_id)
    # Serve the summary AND the free-form chips in the request's language, translating + caching on
    # demand (review F1). The first request in a new language spends one workhorse call; offline
    # serves the stored (canonical) strings. The canonical values in `structured` never change —
    # `displayTerms` is a display-only canonical→display map the frontend looks chips up in.
    display = localized_display(session, taste, language, optional_llm_provider())
    return _to_response(taste, summary=display.summary, display_terms=display.terms)


@router.post("/profiles/{profile_id}/taste/generate", response_model=TasteResponse)
def generate_taste(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    llm: Annotated[LLMProvider, Depends(get_llm_provider)],
    language: Annotated[Language, Depends(get_language)],
) -> TasteResponse:
    require_own_profile(user, profile_id)
    settings = get_settings()
    # Force-regenerate, but rate-limited per profile: a workhorse LLM call is spent each time, so
    # repeated button clicks must not each trigger one. The auto-refresh on ingest keeps taste fresh
    # between manual regenerations.
    existing = session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))
    if existing is not None and existing.generated_at is not None:
        elapsed = (datetime.now(UTC) - existing.generated_at).total_seconds()
        retry_after = int(settings.taste_generate_cooldown_seconds - elapsed)
        if retry_after > 0:
            minutes, seconds = divmod(retry_after, 60)
            human = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
            raise HTTPException(
                status_code=429,
                detail=f"Taste was just regenerated — try again in {human}.",
                headers={"Retry-After": str(retry_after)},
            )
    # The manual "Regenerate" button explicitly asks for an LLM extraction. If the provider can't be
    # reached (transport/HTTP error) or the monthly budget is spent, be honest (principle #4): give
    # a 503 with a structured DTO instead of silently degrading to the deterministic profile (that
    # silent fallback is only right for the *auto*-refresh, which stays untouched). An unparseable
    # completion is still handled inside generate() — the model answered, just not with JSON — so it
    # keeps degrading to the deterministic floor and does not reach here.
    try:
        taste = TasteService(session, llm, settings.llm_chat_model, language).generate(profile_id)
    except LLMBudgetExceeded:
        record_fallback("taste_extraction", "budget_exhausted", profile_id=str(profile_id))
        raise HTTPException(
            status_code=503,
            detail=LLMUnavailable(reason="budget_exhausted").model_dump(by_alias=True),
        ) from None
    except (httpx.HTTPError, ConnectionError, TimeoutError) as exc:
        record_fallback("taste_extraction", "llm_unreachable", profile_id=str(profile_id))
        raise HTTPException(
            status_code=503,
            detail=LLMUnavailable(reason="llm_unreachable").model_dump(by_alias=True),
        ) from exc
    session.commit()
    return _to_response(taste)


# How many exemplar titles a facet carries on the wire — the 3 most centroid-central members.
_FACET_EXEMPLARS = 3


def _facet_label(member_titles: list[Title], fallback: str) -> str:
    """Deterministic facet label from the dominant genres of its member titles.

    Top genre by frequency, joined with a second one only when it's genuinely co-dominant (at least
    half the top genre's count) — "Action · Science Fiction", not a laundry list. Ties break
    alphabetically so the label never flickers between requests. Genres are the catalog's English
    labels; the client localises them for display (it already owns the genre translation table).
    ``fallback`` (the most central member's title) covers the no-genre-metadata edge."""
    counts: Counter[str] = Counter()
    for title in member_titles:
        counts.update(title.genres or [])
    if not counts:
        return fallback
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    top_genre, top_count = ranked[0]
    label = top_genre
    if len(ranked) > 1:
        second_genre, second_count = ranked[1]
        if second_count * 2 >= top_count:
            label = f"{top_genre} · {second_genre}"
    return label


@router.get("/profiles/{profile_id}/taste/facets", response_model=TasteFacetsResponse)
def get_taste_facets(
    profile_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
) -> TasteFacetsResponse:
    """The profile's taste facets — the distinct modes the recommender retrieves for (round 10),
    surfaced so the taste stays inspectable (principle 2). Fully deterministic from the stored
    embeddings: no LLM call, no persistence. A single-facet taste (small history, cohesive taste,
    or no signal at all) returns an empty list — one blob facet carries no insight."""
    require_own_profile(user, profile_id)
    contributions = taste_contributions(session, profile_id, embedder.read_version)
    facets = extract_facets(contributions)
    if len(facets) <= 1:
        logger.info(
            "taste.facets.served",
            extra={"profile_id": str(profile_id), "k": len(facets), "single_mode": True},
        )
        return TasteFacetsResponse(facets=[])

    # One query for every member title across all facets (genres + exemplar metadata) — no N+1.
    member_ids = {title_id for facet in facets for title_id in facet.member_title_ids}
    titles: dict[uuid.UUID, Title] = {
        title.id: title for title in session.scalars(select(Title).where(Title.id.in_(member_ids)))
    }
    items: list[TasteFacetResponse] = []
    for facet in facets:  # extract_facets already orders by weight desc
        ranked = [
            title_id
            for title_id in rank_members_by_centrality(facet, contributions)
            if title_id in titles
        ]
        exemplars = [
            FacetExemplarResponse(
                title_id=title.id,
                title=title.title,
                year=title.year,
                poster_url=_poster_url(title.poster_path),
            )
            for title in (titles[title_id] for title_id in ranked[:_FACET_EXEMPLARS])
        ]
        member_titles = [titles[title_id] for title_id in ranked]
        items.append(
            TasteFacetResponse(
                label=_facet_label(member_titles, fallback=exemplars[0].title if exemplars else ""),
                weight=facet.weight,
                title_count=facet.size,
                exemplars=exemplars,
            )
        )
    logger.info(
        "taste.facets.served",
        extra={
            "profile_id": str(profile_id),
            "k": len(items),
            "labels": [item.label for item in items],
            "weights": [round(item.weight, 3) for item in items],
        },
    )
    return TasteFacetsResponse(facets=items)


@router.put("/profiles/{profile_id}/taste", response_model=TasteResponse)
def update_taste(
    profile_id: uuid.UUID,
    body: UpdateTasteRequest,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> TasteResponse:
    require_own_profile(user, profile_id)
    taste = _require_taste(session, profile_id)
    taste.user_overrides = body.user_overrides
    session.commit()
    return _to_response(taste)
