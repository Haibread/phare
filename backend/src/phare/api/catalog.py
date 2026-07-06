"""Catalog endpoints: seed the offline sample, import from TMDB, embed missing titles."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.deps import Embedder, get_embedder, get_language
from phare.api.recommend import _poster_url
from phare.api.schemas import ApiModel, CatalogSummary, EmbedSummary, RecommendationItem
from phare.catalog.sample import seed_sample_catalog
from phare.catalog.service import import_from_tmdb, search_titles
from phare.core.config import Settings, get_settings
from phare.core.i18n import Language
from phare.db.base import get_session
from phare.db.models import Profile, TasteProfile, Title
from phare.embeddings.service import EmbeddingService
from phare.embeddings.version import embedding_model_version
from phare.providers.tmdb import TMDBMetadataProvider
from phare.recommend.rows import confidences_for_ordered_titles
from phare.recommend.taste_vector import compute_taste_centroid, watched_title_ids
from phare.taste.service import effective_profile

router = APIRouter(tags=["Catalog"])


class SearchRequest(ApiModel):
    q: str


class SearchResponse(ApiModel):
    results: list[RecommendationItem]


def _to_search_item(
    title: Title, *, watched: bool, confidence: float | None = None
) -> RecommendationItem:
    return RecommendationItem(
        title_id=title.id,
        title=title.title,
        kind=title.kind.value,
        year=title.year,
        genres=list(title.genres),
        score=0.0,
        is_swing=False,
        confidence=confidence,
        explanation=None,
        poster_url=_poster_url(title.poster_path),
        components={},
        watched=watched,
    )


@router.post("/profiles/{profile_id}/catalog/search", response_model=SearchResponse)
def search_catalog(
    profile_id: uuid.UUID,
    body: SearchRequest,
    session: Annotated[Session, Depends(get_session)],
    language: Annotated[Language, Depends(get_language)],
) -> SearchResponse:
    """Search the catalog (and live TMDB when configured) so a title can be found + requested."""
    if session.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    settings = get_settings()
    tmdb = (
        TMDBMetadataProvider(
            api_key=settings.tmdb_api_key,
            base_url=settings.tmdb_base_url,
            language=language,
            cache_ttl=settings.tmdb_cache_ttl_seconds,
        )
        if settings.tmdb_api_key
        else None
    )
    titles = search_titles(session, body.q, tmdb, limit=12)
    session.commit()  # persist any titles upserted from TMDB
    watched = watched_title_ids(session, profile_id)  # badge the ones you've already seen (A11)
    # Stamp honest taste-fit on the results, same blend as the popular row — search order is lexical
    # relevance, but the fit gauge reads real taste. Degrades to null (UI hides the gauge) with no
    # taste profile / no embeddings. Small pool (~12), no per-item LLM, one embedding query.
    model_version = embedding_model_version(get_settings())
    centroid = compute_taste_centroid(session, profile_id, model_version)
    taste_row = session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))
    taste = effective_profile(taste_row) if taste_row is not None else {}
    confidences = confidences_for_ordered_titles(
        session, titles, centroid=centroid, taste=taste, model_version=model_version
    )
    return SearchResponse(
        results=[
            _to_search_item(t, watched=t.id in watched, confidence=confidence)
            for t, confidence in zip(titles, confidences, strict=True)
        ]
    )


@router.post("/catalog/sample", response_model=CatalogSummary)
def seed_catalog(session: Annotated[Session, Depends(get_session)]) -> CatalogSummary:
    """Seed the offline sample catalog so the engine has something to recommend (dev only)."""
    if get_settings().environment == "production":
        raise HTTPException(status_code=403, detail="Sample catalog is disabled in production")
    created = seed_sample_catalog(session)
    session.commit()
    return CatalogSummary(created=created)


def ensure_import_allowed(settings: Settings, confirm: bool) -> None:
    """Refuse a TMDB catalog import outside production unless explicitly confirmed, so a dev box
    can't fan out a large fetch by accident. The deep ``broad`` seed runs from the CLI."""
    if not settings.is_production and not confirm:
        raise HTTPException(
            status_code=403,
            detail="Catalog import is disabled outside production; pass confirm=true to override.",
        )


@router.post("/catalog/import", response_model=CatalogSummary)
def import_catalog(
    session: Annotated[Session, Depends(get_session)],
    pages: int = 1,
    confirm: bool = False,
) -> CatalogSummary:
    """Import TMDB's popular movies + shows into the candidate pool.

    Light, page-bounded top-up. For a broad genre-sweep seed, use ``phare import-catalog
    --scope broad`` (a longer-running job, better off the request path).
    """
    settings = get_settings()
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="TMDB_API_KEY must be configured to import")
    ensure_import_allowed(settings, confirm)
    metadata = TMDBMetadataProvider(
        api_key=settings.tmdb_api_key,
        base_url=settings.tmdb_base_url,
        cache_ttl=settings.tmdb_cache_ttl_seconds,
    )
    created = import_from_tmdb(session, metadata, pages=pages)
    session.commit()
    return CatalogSummary(created=created)


@router.post("/catalog/embed", response_model=EmbedSummary)
def embed_catalog(
    session: Annotated[Session, Depends(get_session)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
) -> EmbedSummary:
    """Embed any titles missing a vector for the active embedding space."""
    embedded = EmbeddingService(session, embedder.provider, embedder.model_version).embed_missing()
    session.commit()
    return EmbedSummary(embedded=embedded)
