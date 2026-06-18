"""Catalog ingest: upsert recommendable ``Title`` rows from metadata.

Recommendation candidates live in the same ``Title`` table as watched titles; what makes a
title a "candidate" is simply that the profile has no watch event for it. This module fills
that pool — from the offline sample catalog or from TMDB's popular lists — idempotently by
``tmdb_id`` so re-running never duplicates.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.db.models import Title, TitleKind
from phare.providers.types import TitleMetadata

logger = logging.getLogger(__name__)


class CatalogSource(Protocol):
    """A provider that can list popular titles to seed the candidate pool (e.g. TMDB)."""

    def popular(self, kind: TitleKind, page: int = 1) -> list[TitleMetadata]: ...


class CatalogSearchSource(Protocol):
    """A provider that can search titles by free text (e.g. TMDB)."""

    def search(self, query: str, *, limit: int = ...) -> list[TitleMetadata]: ...


def search_titles(
    session: Session,
    query: str,
    metadata: CatalogSearchSource | None = None,
    *,
    limit: int = 12,
) -> list[Title]:
    """Search the catalog by title. With a TMDB provider, pull live matches in first (upserting
    them so they become recommendable + requestable); always fall back to local substring match."""
    query = query.strip()
    if not query:
        return []
    results: list[Title] = []
    seen: set[uuid.UUID] = set()
    if metadata is not None:
        metas = metadata.search(query, limit=limit)
        upsert_titles(session, metas)
        session.flush()
        for meta in metas:
            if meta.tmdb_id is None:
                continue
            title = session.scalar(select(Title).where(Title.tmdb_id == meta.tmdb_id))
            if title is not None and title.id not in seen:
                seen.add(title.id)
                results.append(title)
    local = session.scalars(
        select(Title)
        .where(Title.title.ilike(f"%{query}%"))
        .order_by(Title.popularity.desc().nulls_last())
        .limit(limit)
    ).all()
    for title in local:
        if title.id not in seen:
            seen.add(title.id)
            results.append(title)
    return results[:limit]


def upsert_titles(session: Session, metas: Iterable[TitleMetadata]) -> int:
    """Insert any titles not already present (by ``tmdb_id``); refresh popularity on the rest.

    Returns the number of newly created titles. Metadata refresh is limited to ``popularity``
    so a re-import keeps the global popularity signal current without clobbering anything an
    embed already depends on (a content change would need a re-embed; out of scope here).
    """
    created = 0
    for meta in metas:
        if meta.tmdb_id is None:
            continue
        existing = session.scalar(select(Title).where(Title.tmdb_id == meta.tmdb_id))
        if existing is not None:
            if meta.popularity is not None:
                existing.popularity = meta.popularity
            # Backfill a poster when we didn't have one; it doesn't affect embeddings.
            if existing.poster_path is None and meta.poster_path is not None:
                existing.poster_path = meta.poster_path
            continue
        session.add(
            Title(
                kind=meta.kind,
                tmdb_id=meta.tmdb_id,
                imdb_id=meta.imdb_id,
                title=meta.title,
                year=meta.year,
                runtime_minutes=meta.runtime_minutes,
                overview=meta.overview,
                poster_path=meta.poster_path,
                genres=meta.genres,
                keywords=meta.keywords,
                popularity=meta.popularity,
            )
        )
        created += 1
    session.flush()
    logger.info("catalog.upsert", extra={"created_count": created})
    return created


def import_from_tmdb(
    session: Session,
    metadata: CatalogSource,
    kinds: Sequence[TitleKind] = (TitleKind.movie, TitleKind.show),
    pages: int = 1,
) -> int:
    """Pull TMDB's popular movies/shows into the catalog. Returns count created."""
    metas: list[TitleMetadata] = []
    for kind in kinds:
        for page in range(1, pages + 1):
            metas.extend(metadata.popular(kind, page))
    return upsert_titles(session, metas)
