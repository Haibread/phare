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

from phare.db.models import TasteProfile
from phare.embeddings.service import EmbeddingService
from phare.providers.types import LLMProvider
from phare.recommend import rows as row_builders
from phare.recommend.candidates import generate_candidates
from phare.recommend.explain import explain
from phare.recommend.log import log_rows
from phare.recommend.reranker import rerank
from phare.recommend.schema import Candidate, Recommendation, Row
from phare.recommend.taste_vector import compute_taste_centroid
from phare.taste.service import effective_profile

logger = logging.getLogger(__name__)

CandidateFilter = Callable[[list[Candidate]], list[Candidate]]


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
    ) -> None:
        self.session = session
        self.embed_provider = embed_provider
        self.embed_model_version = embed_model_version
        self.chat_llm = chat_llm
        self.row_size = row_size
        self.swing_slots = swing_slots

    def ensure_embeddings(self) -> int:
        """Embed any title (catalog or watched) missing a vector for the active space."""
        return EmbeddingService(
            self.session, self.embed_provider, self.embed_model_version
        ).embed_missing()

    def load_taste(self, profile_id: uuid.UUID) -> dict[str, object]:
        """The effective taste profile (structured + sticky overrides), or {} if none yet."""
        taste = self.session.scalar(
            select(TasteProfile).where(TasteProfile.profile_id == profile_id)
        )
        return effective_profile(taste) if taste is not None else {}

    # Backwards-compatible alias used internally.
    _load_taste = load_taste

    def recommend(
        self,
        profile_id: uuid.UUID,
        *,
        taste: dict[str, object] | None = None,
        extra_hard_avoids: Sequence[str] = (),
        candidate_filter: CandidateFilter | None = None,
        k: int | None = None,
        swing_slots: int | None = None,
    ) -> list[Recommendation]:
        """Shared pipeline: centroid -> candidates -> (filter) -> rerank -> explain."""
        if taste is None:
            taste = self._load_taste(profile_id)
        centroid = compute_taste_centroid(self.session, profile_id, self.embed_model_version)
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
        )
        if candidate_filter is not None:
            candidates = candidate_filter(candidates)
        recs = rerank(
            candidates,
            taste,
            k=k,
            swing_slots=swing_slots if swing_slots is not None else self.swing_slots,
        )
        return explain(recs, taste, self.chat_llm)

    def you_might_like(self, profile_id: uuid.UUID) -> Row:
        """The full pipeline. This is the product."""
        items = self.recommend(profile_id)
        return Row(key="you_might_like", title="You might like", items=items)

    def rows(self, profile_id: uuid.UUID) -> list[Row]:
        """The home-screen strip set. Empty rows are kept out so the UI stays clean."""
        self.ensure_embeddings()
        candidate_rows = [
            row_builders.continue_watching_row(self.session, profile_id, limit=self.row_size),
            self.you_might_like(profile_id),
            row_builders.watch_again_row(self.session, profile_id, limit=self.row_size),
            row_builders.popular_row(self.session, profile_id, limit=self.row_size),
        ]
        result = [row for row in candidate_rows if row.items]
        log_rows(self.session, profile_id, result)
        logger.info(
            "recommend.rows",
            extra={"profile_id": str(profile_id), "row_count": len(result)},
        )
        return result
