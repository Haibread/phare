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

from sqlalchemy import and_, func, not_, or_, select, text
from sqlalchemy.orm import Session

from phare.core.fallback import record_fallback
from phare.db.models import Title, TitleEmbedding, TitleKind
from phare.providers.embeddings_local import is_local_space
from phare.providers.types import LLMProvider, MetadataProvider, TitleMetadata

logger = logging.getLogger(__name__)

# Below this vote count (or with none at all) a non-exact lexical match is treated as junk: still
# returned, but only after every above-floor match — and a semantic ANN neighbour must clear the
# same bar. Mirrors the broad-import quality floor (``broad_import_from_tmdb``'s default).
SEARCH_VOTE_FLOOR = 50


class CatalogSource(Protocol):
    """A provider that can list popular titles to seed the candidate pool (e.g. TMDB)."""

    def popular(self, kind: TitleKind, page: int = 1) -> list[TitleMetadata]: ...


class CatalogSearchSource(Protocol):
    """A provider that can search titles by free text (e.g. TMDB)."""

    def search(self, query: str, *, limit: int = ...) -> list[TitleMetadata]: ...


class CatalogMetadataSource(CatalogSearchSource, MetadataProvider, Protocol):
    """A provider that can both search the catalog *and* resolve per-title detail (e.g. TMDB).

    The chat tool context holds one of these: ``search`` resolves a named title, ``get_title``
    backfills a candidate's runtime. Typing it as the union lets both callers stay precise.
    """


class CatalogRefreshSource(Protocol):
    """A provider that can list current/new releases to keep the catalog fresh (e.g. TMDB)."""

    def trending(self, kind: TitleKind, page: int = ...) -> list[TitleMetadata]: ...

    def now_playing(self, kind: TitleKind, page: int = ...) -> list[TitleMetadata]: ...


class CatalogDiscoverSource(Protocol):
    """A provider that can enumerate genres and page a discover endpoint (e.g. TMDB)."""

    def genres(self, kind: TitleKind) -> dict[int, str]: ...

    def discover(
        self,
        kind: TitleKind,
        *,
        genre_id: int | None = ...,
        min_vote_count: int = ...,
        page: int = ...,
    ) -> list[TitleMetadata]: ...


def _below_floor(title: Title) -> bool:
    """The Python twin of the SQL vote-floor predicate, for rows merged outside the ranked query."""
    return title.vote_count is None or title.vote_count < SEARCH_VOTE_FLOOR


def _semantic_fill(
    session: Session,
    query: str,
    embedder: LLMProvider,
    embedding_version: str,
    *,
    exclude: set[uuid.UUID],
    slots: int,
) -> list[Title]:
    """Embedding-nearest catalog titles for the raw query text — the "ghibli" tier.

    One embedding call; ANN over the request's *served* space, above-floor only (a 3-vote
    neighbour is as much junk as a 3-vote substring match), excluding titles the lexical pass
    already returned. Any embed failure degrades to no fill — search never breaks on it."""
    try:
        query_vec = [float(x) for x in embedder.embed([query])[0]]
    except Exception:  # noqa: BLE001 - a query-embed hiccup must not sink the search request
        record_fallback("search", "semantic_embed_error")
        return []
    distance = TitleEmbedding.embedding.cosine_distance(query_vec)
    # Same determinism guard as ``recommend.candidates``: the HNSW index is approximate, so widen
    # the beam and break exact-distance ties on the stable catalog id, or results flicker.
    session.execute(text("SET LOCAL hnsw.ef_search = 1000"))
    stmt = (
        select(Title)
        .join(TitleEmbedding, TitleEmbedding.title_id == Title.id)
        .where(
            TitleEmbedding.model_version == embedding_version,
            Title.vote_count >= SEARCH_VOTE_FLOOR,
        )
        .order_by(distance.asc(), Title.tmdb_id.asc().nulls_last(), Title.id)
        .limit(slots + len(exclude))  # over-fetch: the exclusion filter may eat into the top
    )
    if exclude:
        stmt = stmt.where(Title.id.notin_(exclude))
    fills = list(session.scalars(stmt).all())[:slots]
    logger.info(
        "catalog.search.semantic_fill",
        extra={"query": query, "weak_slots": slots, "filled": len(fills)},
    )
    return fills


def search_titles(
    session: Session,
    query: str,
    metadata: CatalogSearchSource | None = None,
    *,
    limit: int = 12,
    embedder: LLMProvider | None = None,
    embedding_version: str | None = None,
) -> list[Title]:
    """Search the catalog by title. With a TMDB provider, pull live matches in first (upserting
    them so they become recommendable + requestable); always fall back to local substring match.

    With an ``embedder`` + ``embedding_version`` (the request's *served* space tag), weak lexical
    results are topped up with embedding-nearest titles for the query text — so "ghibli" surfaces
    Spirited Away, not just obscure documentaries whose title contains the word. Skipped entirely
    offline (no embedder, or the meaningless local hash space): exact lexical-only behaviour.
    """
    query = query.strip()
    if not query:
        return []
    # TMDB is a *discovery* source, not an ordering source: upsert its matches so they exist
    # locally, then rank everything below with the single lexical-tier + vote-count ordering.
    # Prepending TMDB's own order shadowed that ranking entirely whenever a key was configured
    # (production), which is how "Bikini Inception" outranked a 314-vote match live.
    tmdb_matches: list[Title] = []
    if metadata is not None:
        metas = metadata.search(query, limit=limit)
        upsert_titles(session, metas)
        session.flush()
        for meta in metas:
            if meta.tmdb_id is None:
                continue
            title = session.scalar(
                select(Title).where(Title.tmdb_id == meta.tmdb_id, Title.kind == meta.kind)
            )
            if title is not None:
                tmdb_matches.append(title)
    seen: set[uuid.UUID] = set()
    # Escape LIKE wildcards in the user's query so "%" / "_" match literally, not as patterns.
    like = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    # Rank lexical relevance FIRST, then break ties by known-ness (vote_count) — so an obscure exact
    # title still leads its lexical tier, and only *within* a tier does the better-known title win
    # (kills the junk tail: "Bikini Inception" / soundtrack albums that share a word-start with the
    # good match but have ~no votes). Popularity is dropped as the tiebreak: vote_count is the
    # honest known-ness signal and the one the chat title-resolver already trusts; popularity is a
    # transient TMDB trending number. Tiers, highest first (booleans → desc puts True first):
    #   1. exact title match (the whole title equals the query)
    #   2. word-start match (title begins with the query, or a word inside it does)
    #   3. mid-word substring (the fallback the WHERE clause allows)
    # Within the non-exact tiers, sub-floor matches ("Bikini Inception", 0-vote concert films) are
    # *demoted* below every above-floor match of BOTH tiers — still findable, never on top. The
    # exact tier is deliberately floor-exempt: typing an exact obscure title must still find it.
    exact = func.lower(Title.title) == query.lower()
    word_start = or_(
        Title.title.ilike(f"{like}%", escape="\\"),
        Title.title.ilike(f"% {like}%", escape="\\"),
    )
    demoted = and_(
        not_(exact),
        or_(Title.vote_count.is_(None), Title.vote_count < SEARCH_VOTE_FLOOR),
    )
    rows = session.execute(
        select(Title, demoted.label("demoted"))
        .where(Title.title.ilike(f"%{like}%", escape="\\"))
        .order_by(
            exact.desc(),
            demoted.asc(),  # above-floor (of both non-exact tiers) before any sub-floor match
            word_start.desc(),
            Title.vote_count.desc().nulls_last(),
            Title.tmdb_id.asc().nulls_last(),  # stable final tie-break
        )
        .limit(limit)
    ).all()
    lead: list[Title] = []  # exact matches + above-floor lexical matches, in ranked order
    junk: list[Title] = []  # sub-floor non-exact matches, demoted behind everything better
    for title, is_demoted in rows:
        if title.id in seen:
            continue
        seen.add(title.id)
        (junk if is_demoted else lead).append(title)
    # Fuzzy TMDB matches whose stored title doesn't literally contain the query (translations,
    # alternate titles) aren't in the ranked query above — append them after every lexical match
    # of their group, the same floor deciding which group they land in.
    for title in tmdb_matches:
        if title.id in seen:
            continue
        seen.add(title.id)
        (junk if _below_floor(title) else lead).append(title)
    # Semantic fill tier: when the above-floor lexical yield leaves slots open, fill them with
    # embedding-nearest titles for the query text — after the good lexical matches, *before* the
    # demoted junk (a real ANN neighbour beats a 3-vote substring match). Deterministic trigger:
    # count above-floor results; anything short of ``limit`` is a weak slot.
    fills: list[Title] = []
    strong = sum(1 for title in lead if not _below_floor(title))
    weak_slots = limit - strong
    if (
        weak_slots > 0
        and embedder is not None
        and embedding_version is not None
        # The local hash space carries no semantic meaning — a hashed query vector would only pull
        # in noise, so offline search stays purely lexical (degrade gracefully, principle 5).
        and not is_local_space(embedding_version)
    ):
        fills = _semantic_fill(
            session, query, embedder, embedding_version, exclude=seen, slots=weak_slots
        )
    return [*lead, *fills, *junk][:limit]


def upsert_titles(session: Session, metas: Iterable[TitleMetadata]) -> int:
    """Insert any titles not already present (by ``tmdb_id``); refresh popularity on the rest.

    Returns the number of newly created titles. Metadata refresh is limited to ``popularity``,
    ``vote_count`` and ``vote_average`` so a re-import keeps the global popularity/known-ness/
    quality signals current without clobbering anything an embed already depends on (a content
    change would need a re-embed; out of scope here).
    """
    created = 0
    for meta in metas:
        if meta.tmdb_id is None:
            continue
        existing = session.scalar(
            select(Title).where(Title.tmdb_id == meta.tmdb_id, Title.kind == meta.kind)
        )
        if existing is not None:
            if meta.popularity is not None:
                existing.popularity = meta.popularity
            if meta.vote_count is not None:
                existing.vote_count = meta.vote_count
            if meta.vote_average is not None:
                existing.vote_average = meta.vote_average
            # Backfill a poster when we didn't have one; it doesn't affect embeddings.
            if existing.poster_path is None and meta.poster_path is not None:
                existing.poster_path = meta.poster_path
            # Backfill original language when missing — a re-import (discover carries it) fills the
            # column for free without a heal fetch. Never clobber a known value.
            if existing.original_language is None and meta.original_language is not None:
                existing.original_language = meta.original_language
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
                directors=meta.directors,
                top_cast=meta.top_cast,
                original_language=meta.original_language,
                popularity=meta.popularity,
                vote_count=meta.vote_count,
                vote_average=meta.vote_average,
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


def refresh_from_tmdb(
    session: Session,
    source: CatalogRefreshSource,
    *,
    kinds: Sequence[TitleKind] = (TitleKind.movie, TitleKind.show),
    pages: int = 1,
) -> int:
    """Keep the catalog current: pull *new/current* releases (this week's trending + what's playing
    now / on the air) and upsert them. Returns count created.

    Deliberately **no vote-count floor** — brand-new releases have barely any votes, so a floor (as
    the broad seed uses for quality) would exclude exactly the content this is meant to catch.
    Dedupes the two lists by ``tmdb_id`` and drops anything with no overview (the embed input).
    """
    by_tmdb_id: dict[tuple[int, TitleKind], TitleMetadata] = {}
    for kind in kinds:
        for page in range(1, pages + 1):
            for meta in (*source.trending(kind, page), *source.now_playing(kind, page)):
                if meta.tmdb_id is None or not (meta.overview or "").strip():
                    continue
                by_tmdb_id.setdefault((meta.tmdb_id, meta.kind), meta)
    created = upsert_titles(session, list(by_tmdb_id.values()))
    logger.info(
        "catalog.refresh",
        extra={"created_count": created, "unique_count": len(by_tmdb_id)},
    )
    return created


def broad_import_from_tmdb(
    session: Session,
    source: CatalogDiscoverSource,
    *,
    kinds: Sequence[TitleKind] = (TitleKind.movie, TitleKind.show),
    pages_per_genre: int = 20,
    min_vote_count: int = 50,
) -> int:
    """Seed a *broad* catalog by paging TMDB's discover endpoint per genre, far past the popular
    front page — so similarity search can surface the lesser-known long tail (a Matrix fan's
    Equilibrium), not just blockbusters. Returns count created.

    Sorted by vote count with a floor (``min_vote_count``) for quality; dedupes across genres (a
    title carries several) and drops titles with no overview, since that's the embedding input.
    Stops a genre early once discover runs dry rather than walking all ``pages_per_genre``.
    """
    by_tmdb_id: dict[tuple[int, TitleKind], TitleMetadata] = {}
    for kind in kinds:
        for genre_id in source.genres(kind):
            for page in range(1, pages_per_genre + 1):
                metas = source.discover(
                    kind, genre_id=genre_id, min_vote_count=min_vote_count, page=page
                )
                if not metas:
                    break  # past the last page of results for this genre
                for meta in metas:
                    if meta.tmdb_id is None or not (meta.overview or "").strip():
                        continue
                    by_tmdb_id.setdefault((meta.tmdb_id, meta.kind), meta)
    created = upsert_titles(session, list(by_tmdb_id.values()))
    logger.info(
        "catalog.broad_import",
        extra={
            "created_count": created,
            "unique_count": len(by_tmdb_id),
            "min_vote_count": min_vote_count,
        },
    )
    return created
