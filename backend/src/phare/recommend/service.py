"""Recommendation orchestration: lazy-embed -> centroid -> candidates -> rerank -> explain -> rows.

Holds the engine together and keeps it safe at N=1 (every step degrades to empty rather than
erroring). The ``recommend`` method is the shared core used by both the ``you_might_like`` row
and the chat agent — the chat agent just passes an extra candidate filter for mood/intent.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import Counter
from collections.abc import Callable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.catalog.heal import apply_metadata_heal, fetch_metadata_parallel
from phare.core.fallback import record_fallback
from phare.core.i18n import DEFAULT_LANGUAGE, Language, translate
from phare.db.models import TasteProfile, Title, TitleEmbedding, WatchEvent
from phare.embeddings.backfill import schedule_embedding_backfill
from phare.embeddings.service import EmbeddingService
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION
from phare.providers.types import LLMProvider, MetadataProvider
from phare.recommend import genres
from phare.recommend import rows as row_builders
from phare.recommend.candidates import generate_candidates
from phare.recommend.explain import _EXPLANATION_CACHE, Explainer
from phare.recommend.log import log_rows
from phare.recommend.reranker import rerank
from phare.recommend.schema import Candidate, Recommendation, Row
from phare.recommend.taste_vector import compute_taste_centroid
from phare.taste.service import effective_profile

logger = logging.getLogger(__name__)

CandidateFilter = Callable[[list[Candidate]], list[Candidate]]

# Page hygiene (review A7/A10). A title may appear at most twice across the whole page, applied in
# row-priority order so the strongest rows keep it. And a "because you watched X" row thinner than
# the floor (a single odd neighbour) reads as broken, so it's hidden — the floor targets those
# auxiliary rows, never you_might_like / popular / user-state rows, which are the page's backbone.
_APPEARANCE_BUDGET = 2
MIN_ROW_ITEMS = 3
_BECAUSE_PREFIX = "because:"


def _apply_appearance_budget(rows: list[Row], budget: int) -> list[Row]:
    """Cap each title to ``budget`` appearances across the page, keeping it in the earliest (highest
    priority) rows and dropping the later repeats."""
    counts: Counter[uuid.UUID] = Counter()
    out: list[Row] = []
    for row in rows:
        kept = []
        for item in row.items:
            if counts[item.title_id] < budget:
                kept.append(item)
                counts[item.title_id] += 1
        out.append(row.model_copy(update={"items": kept}))
    return out


# How far a chat mood pulls the taste centroid toward the mood text's embedding (0 = ignore mood,
# 1 = ignore taste). A gentle nudge: taste still leads, the mood colours it (review A4).
_MOOD_WEIGHT = 0.3


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def _blend_direction(a: list[float], b: list[float], weight: float) -> list[float]:
    """Blend two vectors' *directions* — magnitude-agnostic, since candidate ranking is by cosine.
    Result points ``(1-weight)`` toward ``a`` and ``weight`` toward ``b``."""
    ua, ub = _unit(a), _unit(b)
    return [(1.0 - weight) * x + weight * y for x, y in zip(ua, ub, strict=True)]


# Read-path embedding micro-batch. The read path must never embed a fresh import inline (minutes
# against a real embedding API — review C1): it tops up only a tiny batch for the "almost nothing
# missing" case, bounded by BOTH a title count and a wall-clock budget, then hands the rest to a
# background backfill. Authoritative unbounded paths: POST /catalog/embed and the backfill thread.
READ_EMBED_MICRO_LIMIT = 16
READ_EMBED_MICRO_BATCH = 8  # sub-batch so the time budget can cut in before the whole micro-limit
READ_EMBED_TIME_BUDGET_S = 2.0

# Read-path runtime backfill. The broad catalog import comes from TMDB *discover*, which omits
# runtime, so freshly-imported titles have ``runtime_minutes = NULL`` and a "something short"
# request has nothing to filter on. We fill the *candidate pool's* missing runtimes from TMDB, but
# only on a turn that actually asks for a runtime cap (so the detail calls aren't spent otherwise),
# in parallel (httpx is thread-safe), and persist each — a one-time cost per title that heals the
# catalog as it's used. The cap is a safety bound; the pool is already small (~k*4+10).
READ_RUNTIME_CAP = 64

# Adaptive constraint-aware re-fetch. ``generate_candidates`` retrieves the ~nearest-to-centroid
# titles with no genre/runtime awareness, then a ``candidate_filter`` (chat intent, dynamic theme)
# prunes them post-hoc. For a taste centred on, say, cerebral thrillers, almost none of those
# nearest neighbours are comedies, so a "light comedy" turn is left with a handful — the runtime cap
# and relevance floor then reduce it to ~1 (round-7 finding 1). When the filtered pool falls below
# what the reranker needs, we re-run the query with the constraint pushed into SQL (array-overlap on
# resolved genre labels + a runtime ceiling), searching the *right* subspace instead of hoping the
# taste-nearest slice happens to contain matches. It still ranks by distance to the centroid within
# that subspace, and the honest relevance floor is untouched — a genuinely thin catalog stays thin.
_REFETCH_POOL_FLOOR_MULT = 1  # re-fetch when the filtered pool has fewer than this * k candidates


class RecommendationService:
    """Builds rows and ad-hoc recommendations for one engine configuration."""

    def __init__(
        self,
        session: Session,
        *,
        embed_provider: LLMProvider,
        embed_model_version: str,
        chat_llm: LLMProvider | None = None,
        row_size: int = 12,
        swing_slots: int = 2,
        explanation_budget: int = 0,  # 0 = template rows; LLM "why this" is lazy per title
        language: Language = DEFAULT_LANGUAGE,
    ) -> None:
        self.session = session
        self.embed_provider = embed_provider
        self.embed_model_version = embed_model_version
        self.chat_llm = chat_llm
        self.row_size = row_size
        self.swing_slots = swing_slots
        self.language = language
        # Per-home-render cap on uncached LLM explanation calls (see explain.Explainer).
        self.explanation_budget = explanation_budget
        # Request-scoped centroid cache: rows()/dynamic_rows fan out many candidate queries off
        # the same centroid, and computing it re-reads every watch event. One service instance is
        # built per request (see api.deps), so this stays correct.
        self._centroid_cache: dict[uuid.UUID, list[float] | None] = {}

    def ensure_embeddings(self) -> int:
        """Non-blocking top-up of missing vectors for the active space. Returns titles embedded now.

        Embeds only a tiny inline micro-batch (bounded by count *and* wall clock) so the read path
        stays fast; if more titles are still missing afterwards, hands the backlog to a single
        background backfill and returns immediately (review C1). The "still building" state that
        results surfaces to the UI via :meth:`profile_building`.
        """
        svc = EmbeddingService(self.session, self.embed_provider, self.embed_model_version)
        embedded = svc.embed_missing(
            batch_size=READ_EMBED_MICRO_BATCH,
            limit=READ_EMBED_MICRO_LIMIT,
            time_budget_s=READ_EMBED_TIME_BUDGET_S,
        )
        if svc.has_missing():
            schedule_embedding_backfill(self.embed_provider, self.embed_model_version)
        return embedded

    def _enrich_runtimes(
        self, candidates: list[Candidate], source: MetadataProvider
    ) -> list[Candidate]:
        """Fill the candidate pool's missing runtimes from the metadata provider, so a runtime cap
        can actually filter. Fetches in parallel and persists each runtime permanently, returning
        the pool with the fetched values applied. The per-title fetch + heal is the shared helper in
        :mod:`phare.catalog.heal` (it also heals a missing ``vote_average`` / ``vote_count``, left
        NULL by the broad discover import, which silently disabled the re-ranker's quality floor).
        Best-effort per title; the caller owns the commit.
        """
        missing = [c for c in candidates if c.runtime_minutes is None][:READ_RUNTIME_CAP]
        if not missing:  # pool already fully runtime'd (the common case once enriched) — no DB hit
            return candidates
        rows = (
            self.session.execute(select(Title).where(Title.id.in_([c.title_id for c in missing])))
            .scalars()
            .all()
        )
        by_id = {row.id: row for row in rows}
        runtimes: dict[uuid.UUID, int] = {}
        for title_id, meta in fetch_metadata_parallel(source, rows).items():
            row = by_id[title_id]  # main thread → safe to write
            if apply_metadata_heal(row, meta) and row.runtime_minutes is not None:
                runtimes[title_id] = row.runtime_minutes
        if not runtimes:
            return candidates
        logger.info("recommend.runtimes_enriched", extra={"enriched_count": len(runtimes)})
        return [
            c.model_copy(update={"runtime_minutes": runtimes[c.title_id]})
            if c.title_id in runtimes
            else c
            for c in candidates
        ]

    def _bias_toward_mood(self, centroid: list[float], mood: str) -> list[float]:
        """Nudge the taste centroid toward a chat mood by blending in the mood text's embedding
        direction — the cheap "LLM steers, embeddings rank" hook for an ephemeral mood (review A4).
        One embedding call, no extra completion. Skipped on the local hash embedder (its vectors
        carry no meaning, so the blend would only add noise) and if the embed call fails.
        """
        if self.embed_model_version == LOCAL_MODEL_VERSION:
            return centroid
        try:
            mood_vec = self.embed_provider.embed([mood])[0]
        except Exception:  # noqa: BLE001 - a mood-embed hiccup must not sink the recommend turn
            record_fallback("mood_bias", "embed_error")
            return centroid
        return _blend_direction(centroid, [float(x) for x in mood_vec], _MOOD_WEIGHT)

    def profile_building(self, profile_id: uuid.UUID) -> bool:
        """True when the profile has watch history but no taste centroid yet — its titles aren't
        embedded, so the personalised rows are empty right after connecting a source. The UI shows a
        "building your profile" state (with the popular row as a stopgap) instead of a bare page at
        the most critical moment (review A12). Call after ``rows()`` so the lazy top-up has run."""
        if self._centroid(profile_id) is not None:
            return False
        return (
            self.session.scalar(
                select(WatchEvent.id)
                .where(WatchEvent.profile_id == profile_id, WatchEvent.excluded.is_(False))
                .limit(1)
            )
            is not None
        )

    def _centroid(self, profile_id: uuid.UUID) -> list[float] | None:
        """Memoized taste centroid for this request."""
        if profile_id not in self._centroid_cache:
            self._centroid_cache[profile_id] = compute_taste_centroid(
                self.session, profile_id, self.embed_model_version
            )
        return self._centroid_cache[profile_id]

    def load_taste(self, profile_id: uuid.UUID) -> dict[str, object]:
        """The effective taste profile (structured + sticky overrides), or {} if none yet."""
        taste = self.session.scalar(
            select(TasteProfile).where(TasteProfile.profile_id == profile_id)
        )
        return effective_profile(taste) if taste is not None else {}

    # Backwards-compatible alias used internally.
    _load_taste = load_taste

    def _explainer(self, *, with_llm: bool, budget: int | None = None) -> Explainer:
        """An explainer sharing the process-wide blurb cache. ``budget`` caps uncached LLM calls
        for this render (None = unbounded, for single-item/ad-hoc calls). The budget is spent on the
        top-ranked items first (rows render most-personalized first), so the visible top picks get
        the blurb and the cache propagates it to the same titles in lower rows."""
        return Explainer(
            llm=self.chat_llm if with_llm else None,
            cache=_EXPLANATION_CACHE,
            budget=budget if budget is not None else 1_000_000_000,
            language=self.language,
        )

    def recommend(
        self,
        profile_id: uuid.UUID,
        *,
        taste: dict[str, object] | None = None,
        extra_hard_avoids: Sequence[str] = (),
        candidate_filter: CandidateFilter | None = None,
        include_genres: Sequence[str] = (),
        max_runtime: int | None = None,
        k: int | None = None,
        swing_slots: int | None = None,
        rewatch: bool = False,
        vote_mix: bool = False,
        explain_with_llm: bool = True,
        explainer: Explainer | None = None,
        runtime_source: MetadataProvider | None = None,
        mood: str | None = None,
    ) -> list[Recommendation]:
        """Shared pipeline: centroid -> candidates -> (filter) -> rerank -> explain.

        ``rewatch=True`` draws candidates from titles the profile has already watched (a comfort
        rewatch) instead of excluding them, and reserves no swing slots — a rewatch is never a
        discovery pick. ``explain_with_llm=False`` uses the deterministic template explanations
        (no per-item LLM call) — the chat agent does this since its natural-language reply already
        frames the picks. Pass a shared ``explainer`` to pool the LLM-call budget across rows.

        ``include_genres`` / ``max_runtime`` are the *structured* form of the constraint the
        ``candidate_filter`` applies in memory. They're what lets the adaptive re-fetch push the
        constraint into SQL when the post-hoc filter starves the pool (see
        :meth:`_constrained_candidates`); the caller passes both — the filter still does the honest
        fine-grained pruning (runtime-unknown handling, kind), the structured pair only steers
        retrieval.
        """
        if taste is None:
            taste = self._load_taste(profile_id)
        centroid = self._centroid(profile_id)
        if centroid is None:
            return []
        if mood:
            centroid = self._bias_toward_mood(centroid, mood)

        avoids = [*(taste.get("hard_avoids") or []), *extra_hard_avoids]
        k = k if k is not None else self.row_size
        candidates = self._constrained_candidates(
            profile_id,
            centroid,
            k=k,
            avoids=avoids,
            rewatch=rewatch,
            candidate_filter=candidate_filter,
            include_genres=include_genres,
            max_runtime=max_runtime,
            runtime_source=runtime_source,
        )
        default_swings = 0 if rewatch else self.swing_slots
        recs = rerank(
            candidates,
            taste,
            k=k,
            swing_slots=swing_slots if swing_slots is not None else default_swings,
            vote_mix=vote_mix,
        )
        exp = explainer or self._explainer(with_llm=explain_with_llm)
        return exp.explain(recs, taste)

    def _constrained_candidates(
        self,
        profile_id: uuid.UUID,
        centroid: list[float],
        *,
        k: int,
        avoids: Sequence[str],
        rewatch: bool,
        candidate_filter: CandidateFilter | None,
        include_genres: Sequence[str],
        max_runtime: int | None,
        runtime_source: MetadataProvider | None,
    ) -> list[Candidate]:
        """Retrieve + filter the candidate pool, adaptively re-searching the constrained subspace
        when the post-hoc filter starves it (round-7 finding 1).

        First pass is the historical one: pull the ~nearest-to-centroid titles, backfill missing
        runtimes, then apply the opaque ``candidate_filter``. If that leaves fewer than ``k`` — a
        taste centred elsewhere than the requested genre yields almost no matches in the nearest
        slice — and there's a structured constraint to push down, re-run the ANN with the genre /
        runtime constraint in SQL, so it ranks the nearest titles *within the matching subspace*
        rather than hoping the global-nearest slice contained matches. The re-fetched pool goes
        through the same runtime enrichment + filter, and we keep whichever pool the filter left
        larger — never padding with weak matches, so a genuinely thin catalog stays honestly thin.
        """

        def retrieve(genre_labels: Sequence[str], runtime_cap: int | None) -> list[Candidate]:
            pool = generate_candidates(
                self.session,
                profile_id,
                centroid,
                self.embed_model_version,
                limit=k * 4 + 10,
                hard_avoids=avoids,
                from_watched=rewatch,
                genre_labels=genre_labels,
                max_runtime=runtime_cap,
            )
            # Backfill the pool's missing runtimes before filtering, so a runtime cap has data to
            # bite on (the broad import omits runtime). Only on a runtime turn — see tool_recommend.
            if runtime_source is not None:
                pool = self._enrich_runtimes(pool, runtime_source)
            return candidate_filter(pool) if candidate_filter is not None else pool

        filtered = retrieve((), None)
        if len(filtered) >= k * _REFETCH_POOL_FLOOR_MULT:
            return filtered
        # Resolve the intent genres to literal catalog labels so the SQL overlap honours the same
        # alias semantics as the in-memory match (an empty result means the words match no catalog
        # genre — no point re-searching for a label that isn't there).
        genre_labels = (
            genres.resolve_catalog_genres(include_genres, self._catalog_genre_vocabulary())
            if include_genres
            else []
        )
        if not genre_labels and max_runtime is None:
            return filtered  # nothing structured to push into SQL — the thin pool is honest
        refetched = retrieve(genre_labels, max_runtime)
        logger.info(
            "recommend.candidates_refetched",
            extra={
                "profile_id": str(profile_id),
                "first_pass": len(filtered),
                "refetched": len(refetched),
                "genre_labels": genre_labels,
                "max_runtime": max_runtime,
            },
        )
        return refetched if len(refetched) > len(filtered) else filtered

    def _catalog_genre_vocabulary(self) -> list[str]:
        """The distinct genre labels present in the catalog — the vocabulary the intent genres are
        resolved against for the SQL-side filter. Unnest the ``genres`` arrays and dedup in SQL so
        it stays one cheap query, not a full-table scan into Python."""
        rows = self.session.execute(select(func.distinct(func.unnest(Title.genres)))).scalars()
        return [g for g in rows if g]

    def you_might_like(self, profile_id: uuid.UUID, *, explainer: Explainer | None = None) -> Row:
        """The full pipeline. This is the product."""
        items = self.recommend(profile_id, explainer=explainer)
        return Row(
            key="you_might_like", title=translate(self.language, "row.youMightLike"), items=items
        )

    @staticmethod
    def _dedup_against(row: Row, seen: set[uuid.UUID]) -> Row:
        """Drop a row's items already shown in an earlier row, recording the survivors in ``seen``.
        Keeps each title to its strongest (earliest) row so the page isn't the same picks over."""
        kept = [item for item in row.items if item.title_id not in seen]
        seen.update(item.title_id for item in kept)
        return row.model_copy(update={"items": kept})

    def _title_vector(self, title_id: uuid.UUID) -> list[float] | None:
        embedding = self.session.scalar(
            select(TitleEmbedding.embedding).where(
                TitleEmbedding.title_id == title_id,
                TitleEmbedding.model_version == self.embed_model_version,
            )
        )
        return [float(x) for x in embedding] if embedding is not None else None

    def because_you_watched_rows(
        self, profile_id: uuid.UUID, *, max_rows: int = 3, explainer: Explainer | None = None
    ) -> list[Row]:
        """One row per loved title: catalog picks nearest to *that title's* embedding. Similarity-
        led (no swing slots) so the row reads honestly as "because you watched X"."""
        taste = self._load_taste(profile_id)
        avoids = list(taste.get("hard_avoids") or [])
        exp = explainer or self._explainer(with_llm=True)
        rows: list[Row] = []
        for seed in row_builders.loved_seed_titles(self.session, profile_id, limit=max_rows):
            vector = self._title_vector(seed.id)
            if vector is None:
                continue
            candidates = generate_candidates(
                self.session,
                profile_id,
                vector,
                self.embed_model_version,
                limit=self.row_size * 3 + 6,
                hard_avoids=avoids,
            )
            items = exp.explain(rerank(candidates, taste, k=self.row_size, swing_slots=0), taste)
            if items:
                rows.append(
                    Row(
                        key=f"because:{seed.id}",
                        title=translate(self.language, "row.becauseYouWatched", title=seed.title),
                        items=items,
                    )
                )
        return rows

    def rows(self, profile_id: uuid.UUID) -> list[Row]:
        """The home-screen strip set. Empty rows are kept out so the UI stays clean."""
        self.ensure_embeddings()
        taste = self._load_taste(profile_id)
        avoids = list(taste.get("hard_avoids") or [])
        # One explainer per render, so the LLM-call budget is pooled across every explaining row
        # (because-you-watched + you-might-like) instead of each row fanning out independently.
        explainer = self._explainer(with_llm=True, budget=self.explanation_budget)
        # Dedup the taste-driven discovery rows against each other: with a small catalog every
        # "because you watched X" row otherwise leads with the same handful of titles, so the page
        # reads as one row repeated. Each title appears in only its strongest row; the watched-title
        # rows (continue/again) and popular draw from different pools and stay untouched.
        seen: set[uuid.UUID] = set()
        because = [
            self._dedup_against(row, seen)
            for row in self.because_you_watched_rows(profile_id, explainer=explainer)
        ]
        you_might_like = self._dedup_against(
            self.you_might_like(profile_id, explainer=explainer), seen
        )
        continue_watching = row_builders.continue_watching_row(
            self.session, profile_id, limit=self.row_size, language=self.language
        )
        candidate_rows = [
            # Most-personalized first — these render right under the hero top pick.
            *because,
            continue_watching,
            you_might_like,
            # A show you're partway through belongs in continue_watching, not both (review A11).
            row_builders.watch_again_row(
                self.session,
                profile_id,
                limit=self.row_size,
                language=self.language,
                exclude_ids=[item.title_id for item in continue_watching.items],
            ),
            row_builders.popular_row(
                self.session,
                profile_id,
                limit=self.row_size,
                language=self.language,
                hard_avoids=avoids,
                # Honest fit (R6b): score the popularity-ordered picks against the profile's real
                # taste. No centroid (cold start) → confidence stays null → neutral "worth a look".
                centroid=self._centroid(profile_id),
                taste=taste,
                model_version=self.embed_model_version,
            ),
        ]
        # Cap cross-row repeats (The Wire in hero + you_might_like + popular + theme — review A7),
        # then drop any "because you watched X" row left too thin to look intentional (A7/A10).
        budgeted = _apply_appearance_budget(candidate_rows, _APPEARANCE_BUDGET)
        # When hard-avoids thin "popular" (M10.1), hold it to the same minimum as the taste rows so
        # a near-empty leftover isn't shown looking broken; with no avoids the row is untouched.
        thin_gated = {_BECAUSE_PREFIX.rstrip(":"), *({"popular"} if avoids else set())}
        result = [
            row
            for row in budgeted
            if row.items
            and not (row.key.split(":", 1)[0] in thin_gated and len(row.items) < MIN_ROW_ITEMS)
        ]
        log_rows(self.session, profile_id, result)
        logger.info(
            "recommend.rows",
            extra={"profile_id": str(profile_id), "row_count": len(result)},
        )
        return result
