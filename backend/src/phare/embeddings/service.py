"""Title embedding pipeline.

Builds the embedding input from a title's metadata, embeds via the LLM provider, and stores
the vector stamped with the embedding model version. Titles missing an embedding for the
current model version are (re-)embedded; this is how a model change triggers a re-embed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from phare.core.fallback import record_fallback
from phare.db.models import Title, TitleEmbedding
from phare.providers.types import LLMProvider

logger = logging.getLogger(__name__)


def build_embedding_text(title: Title) -> str:
    """Compose the text fed to the embedding model. Quality here = similarity quality."""
    parts: list[str] = [title.title]
    if title.year:
        parts.append(f"Year: {title.year}")
    if title.genres:
        parts.append("Genres: " + ", ".join(title.genres))
    if title.keywords:
        parts.append("Keywords: " + ", ".join(title.keywords))
    if title.overview:
        parts.append(title.overview)
    return "\n".join(parts)


class EmbeddingService:
    """Embeds titles and persists vectors for a given model version."""

    def __init__(self, session: Session, llm: LLMProvider, model_version: str) -> None:
        self.session = session
        self.llm = llm
        self.model_version = model_version

    def _titles_missing_embedding(self, limit: int | None = None) -> Sequence[Title]:
        embedded = (
            select(TitleEmbedding.title_id)
            .where(TitleEmbedding.model_version == self.model_version)
            .scalar_subquery()
        )
        stmt = select(Title).where(Title.id.notin_(embedded))
        if limit is not None:
            stmt = stmt.limit(limit)
        return self.session.scalars(stmt).all()

    def has_missing(self) -> bool:
        """True if any title still lacks a vector for the active model version."""
        return len(self._titles_missing_embedding(limit=1)) > 0

    def embed_missing(
        self,
        batch_size: int = 64,
        limit: int | None = None,
        time_budget_s: float | None = None,
    ) -> int:
        """Embed titles lacking a vector for the current model version. Returns titles embedded.

        ``limit`` bounds how many are embedded in one pass (the lazy read-path micro-batch uses it
        so a big import can't hang a request); ``None`` embeds the whole backlog (the authoritative
        and background paths). ``time_budget_s`` stops the pass early once the budget is spent —
        checked between batches so the first batch always runs — for the same read-path guard.
        """
        titles = list(self._titles_missing_embedding(limit=limit))
        if limit is not None and len(titles) == limit:
            # The read-path micro-batch hit its cap — more titles remain unembedded, so a background
            # backfill takes over; this render only covers part of the catalog (review A15/C1).
            record_fallback("embeddings", "backfill_deferred", embedded_count=limit)
        deadline = None if time_budget_s is None else time.monotonic() + time_budget_s
        embedded = 0
        for start in range(0, len(titles), batch_size):
            # Between batches, bail if the time budget is spent (the first batch, start == 0, always
            # runs — a micro-batch that embeds nothing would be pointless).
            if start > 0 and deadline is not None and time.monotonic() >= deadline:
                break
            batch = titles[start : start + batch_size]
            vectors = self.llm.embed([build_embedding_text(t) for t in batch])
            rows = [
                {"title_id": title.id, "model_version": self.model_version, "embedding": vector}
                for title, vector in zip(batch, vectors, strict=True)
            ]
            # ON CONFLICT DO NOTHING: a concurrent read-path micro-batch can embed the same titles
            # between our SELECT and this INSERT (both lazy passes snapshot the same "missing" set).
            # Converge silently instead of raising a duplicate-key IntegrityError that poisons the
            # transaction. The vector is identical either way, so the race's loser loses nothing.
            self.session.execute(
                pg_insert(TitleEmbedding)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["title_id", "model_version"])
            )
            self.session.flush()
            embedded += len(batch)
        logger.info(
            "embeddings.done",
            extra={"embedded_count": embedded, "model_version": self.model_version},
        )
        return embedded
