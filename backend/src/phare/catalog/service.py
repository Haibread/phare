"""Catalog ingest: upsert recommendable ``Title`` rows from metadata.

Recommendation candidates live in the same ``Title`` table as watched titles; what makes a
title a "candidate" is simply that the profile has no watch event for it. This module fills
that pool — from the offline sample catalog or from TMDB's popular lists — idempotently by
``tmdb_id`` so re-running never duplicates.
"""

from __future__ import annotations

import logging
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
