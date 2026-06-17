"""Catalog endpoints: seed the offline sample, import from TMDB, embed missing titles."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from phare.api.deps import Embedder, get_embedder
from phare.api.schemas import CatalogSummary, EmbedSummary
from phare.catalog.sample import seed_sample_catalog
from phare.catalog.service import import_from_tmdb
from phare.core.config import get_settings
from phare.db.base import get_session
from phare.embeddings.service import EmbeddingService
from phare.providers.tmdb import TMDBMetadataProvider

router = APIRouter(tags=["Catalog"])


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
    metadata = TMDBMetadataProvider(api_key=settings.tmdb_api_key, base_url=settings.tmdb_base_url)
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
