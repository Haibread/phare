"""Catalog endpoints: seed the offline sample, import from TMDB, embed missing titles."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from phare.api.deps import Embedder, get_embedder
from phare.api.recommend import _poster_url
from phare.api.schemas import ApiModel, CatalogSummary, EmbedSummary, RecommendationItem
from phare.catalog.sample import seed_sample_catalog
from phare.catalog.service import import_from_tmdb, search_titles
from phare.core.config import get_settings
from phare.db.base import get_session
from phare.db.models import Profile, Title
from phare.embeddings.service import EmbeddingService
from phare.providers.tmdb import TMDBMetadataProvider

router = APIRouter(tags=["Catalog"])


class SearchRequest(ApiModel):
    q: str


class SearchResponse(ApiModel):
    results: list[RecommendationItem]


def _to_search_item(title: Title) -> RecommendationItem:
    return RecommendationItem(
        title_id=title.id,
        title=title.title,
        kind=title.kind.value,
        year=title.year,
        genres=list(title.genres),
        score=0.0,
        is_swing=False,
        confidence=None,
        explanation=None,
        poster_url=_poster_url(title.poster_path),
        components={},
    )


@router.post("/profiles/{profile_id}/catalog/search", response_model=SearchResponse)
def search_catalog(
    profile_id: uuid.UUID,
    body: SearchRequest,
    session: Annotated[Session, Depends(get_session)],
) -> SearchResponse:
    """Search the catalog (and live TMDB when configured) so a title can be found + requested."""
    if session.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    settings = get_settings()
    tmdb = (
        TMDBMetadataProvider(
            api_key=settings.tmdb_api_key,
            base_url=settings.tmdb_base_url,
            cache_ttl=settings.tmdb_cache_ttl_seconds,
        )
        if settings.tmdb_api_key
        else None
    )
    titles = search_titles(session, body.q, tmdb, limit=12)
    session.commit()  # persist any titles upserted from TMDB
    return SearchResponse(results=[_to_search_item(t) for t in titles])


@router.post("/catalog/sample", response_model=CatalogSummary)
def seed_catalog(session: Annotated[Session, Depends(get_session)]) -> CatalogSummary:
    """Seed the offline sample catalog so the engine has something to recommend (dev only)."""
    if get_settings().environment == "production":
        raise HTTPException(status_code=403, detail="Sample catalog is disabled in production")
    created = seed_sample_catalog(session)
    session.commit()
    return CatalogSummary(created=created)


@router.post("/catalog/import", response_model=CatalogSummary)
def import_catalog(
    session: Annotated[Session, Depends(get_session)],
    pages: int = 1,
) -> CatalogSummary:
    """Import TMDB's popular movies + shows into the candidate pool."""
    settings = get_settings()
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="TMDB_API_KEY must be configured to import")
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
