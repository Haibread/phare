"""Catalog endpoints: seed the offline sample, import from TMDB, embed missing titles."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.deps import Embedder, get_embedder, get_language, get_optional_chat_llm
from phare.api.recommend import _poster_url, localize_items
from phare.api.schemas import ApiModel, CatalogSummary, EmbedSummary, RecommendationItem
from phare.catalog.sample import seed_sample_catalog
from phare.catalog.service import import_from_tmdb, search_titles
from phare.core.config import Settings, get_settings
from phare.core.i18n import Language
from phare.db.base import get_session
from phare.db.models import Profile, TasteProfile, Title
from phare.embeddings.service import EmbeddingService
from phare.providers.tmdb import TMDBMetadataProvider
from phare.providers.types import LLMProvider
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
        # Known-ness/quality straight off the Title row, so search cards can show a compact rating.
        vote_average=title.vote_average,
        vote_count=title.vote_count,
    )


@router.post("/profiles/{profile_id}/catalog/search", response_model=SearchResponse)
def search_catalog(
    profile_id: uuid.UUID,
    body: SearchRequest,
    session: Annotated[Session, Depends(get_session)],
    language: Annotated[Language, Depends(get_language)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    chat_llm: Annotated[LLMProvider | None, Depends(get_optional_chat_llm)],
) -> SearchResponse:
    """Search the catalog (and live TMDB when configured) so a title can be found + requested."""
    if session.get(Profile, profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    settings = get_settings()
    # Language-bound: live TMDB search must match a title typed in the user's language ("Amour
    # éternel" → Kara Sevda). What gets *persisted* for a new title is canonical — search_titles
    # deep-fetches through the provider's canonical() view before upserting.
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
    # The embedder powers the semantic fill tier ("ghibli" → Spirited Away) when lexical results
    # are weak; ``read_version`` is the *served* space, the same one the fit stamping queries.
    # The workhorse LLM translates a non-English query to English before the fill embeds it
    # (catalog documents are English); cached in-process, and never called on English requests.
    titles = search_titles(
        session,
        body.q,
        tmdb,
        limit=12,
        embedder=embedder.provider,
        embedding_version=embedder.read_version,
        translator=chat_llm,
        language=language,
    )
    session.commit()  # persist any titles upserted from TMDB
    watched = watched_title_ids(session, profile_id)  # badge the ones you've already seen (A11)
    # Stamp honest taste-fit on the results, same blend as the popular row — search order is lexical
    # relevance, but the fit gauge reads real taste. Degrades to null (UI hides the gauge) with no
    # taste profile / no embeddings. Small pool (~12), no per-item LLM, one embedding query.
    # Score search fit in the *served* space (coverage-resolved), so the gauge and the home rows
    # read the same vectors — never the write-target space that may still be building.
    model_version = embedder.read_version
    centroid = compute_taste_centroid(session, profile_id, model_version)
    taste_row = session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))
    taste = effective_profile(taste_row) if taste_row is not None else {}
    confidences = confidences_for_ordered_titles(
        session, titles, centroid=centroid, taste=taste, model_version=model_version
    )
    results = [
        _to_search_item(t, watched=t.id in watched, confidence=confidence)
        for t, confidence in zip(titles, confidences, strict=True)
    ]
    # Localized display names for the result cards, bulk from the cache (one query, no TMDB).
    localize_items(session, language, results)
    return SearchResponse(results=results)


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
    """Embed any titles missing a vector for the current-document (write) embedding space."""
    embedded = EmbeddingService(session, embedder.provider, embedder.write_version).embed_missing()
    session.commit()
    return EmbedSummary(embedded=embedded)
