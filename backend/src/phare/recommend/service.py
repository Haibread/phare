"""Recommendation orchestration: lazy-embed -> centroid -> candidates -> rerank -> explain -> rows.

Holds the engine together and keeps it safe at N=1 (every step degrades to empty rather than
erroring). The ``recommend`` method is the shared core used by both the ``you_might_like`` row
and the chat agent — the chat agent just passes an extra candidate filter for mood/intent.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.core.i18n import DEFAULT_LANGUAGE, Language, translate
from phare.db.models import TasteProfile, TitleEmbedding
from phare.embeddings.service import EmbeddingService
from phare.providers.types import LLMProvider
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

# Read-path embedding cap. The authoritative embed path is POST /catalog/embed (unbounded); the
# lazy read-path top-up must stay bounded so a fresh import can't make the first request embed the
# whole catalog inline (minutes against a real embedding API). Beyond the cap we log and defer.
READ_EMBED_CAP = 512


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
        """Bounded lazy top-up of missing vectors for the active space.

        Caps at ``READ_EMBED_CAP`` so the read path can't hang embedding a whole fresh import;
        run ``POST /catalog/embed`` for the authoritative, unbounded pass.
        """
        return EmbeddingService(
            self.session, self.embed_provider, self.embed_model_version
        ).embed_missing(limit=READ_EMBED_CAP)

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
        k: int | None = None,
        swing_slots: int | None = None,
        rewatch: bool = False,
        vote_mix: bool = False,
        explain_with_llm: bool = True,
        explainer: Explainer | None = None,
    ) -> list[Recommendation]:
        """Shared pipeline: centroid -> candidates -> (filter) -> rerank -> explain.

        ``rewatch=True`` draws candidates from titles the profile has already watched (a comfort
        rewatch) instead of excluding them, and reserves no swing slots — a rewatch is never a
        discovery pick. ``explain_with_llm=False`` uses the deterministic template explanations
        (no per-item LLM call) — the chat agent does this since its natural-language reply already
        frames the picks. Pass a shared ``explainer`` to pool the LLM-call budget across rows.
        """
        if taste is None:
            taste = self._load_taste(profile_id)
        centroid = self._centroid(profile_id)
        if centroid is None:
            return []

        avoids = [*(taste.get("hard_avoids") or []), *extra_hard_avoids]
        k = k if k is not None else self.row_size
        candidates = generate_candidates(
            self.session,
            profile_id,
            centroid,
            self.embed_model_version,
            limit=k * 4 + 10,
            hard_avoids=avoids,
            from_watched=rewatch,
        )
        if candidate_filter is not None:
            candidates = candidate_filter(candidates)
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
        candidate_rows = [
            # Most-personalized first — these render right under the hero top pick.
            *because,
            row_builders.continue_watching_row(
                self.session, profile_id, limit=self.row_size, language=self.language
            ),
            you_might_like,
            row_builders.watch_again_row(
                self.session, profile_id, limit=self.row_size, language=self.language
            ),
            row_builders.popular_row(
                self.session, profile_id, limit=self.row_size, language=self.language
            ),
        ]
        result = [row for row in candidate_rows if row.items]
        log_rows(self.session, profile_id, result)
        logger.info(
            "recommend.rows",
            extra={"profile_id": str(profile_id), "row_count": len(result)},
        )
        return result
