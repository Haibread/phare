"""Title embedding pipeline.

Builds the embedding input from a title's metadata, embeds via the LLM provider, and stores
the vector stamped with the embedding model version. Titles missing an embedding for the
current model version are (re-)embedded; this is how a model change triggers a re-embed.
"""

from __future__ import annotations

import logging
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

    def embed_missing(self, batch_size: int = 64, limit: int | None = None) -> int:
        """Embed titles lacking a vector for the current model version. Returns titles processed.

        ``limit`` bounds how many are embedded in one pass (the lazy read-path top-up uses it so a
        big import can't hang a request); ``None`` embeds the whole backlog (authoritative path).
        """
        titles = list(self._titles_missing_embedding(limit=limit))
        if limit is not None and len(titles) == limit:
            # The read-path top-up hit its cap — more titles remain unembedded, so this render's
            # recommendations only cover part of the catalog (review A15/G1).
            record_fallback("embeddings", "backfill_deferred", embedded_count=limit)
        for start in range(0, len(titles), batch_size):
            batch = titles[start : start + batch_size]
            vectors = self.llm.embed([build_embedding_text(t) for t in batch])
            rows = [
                {"title_id": title.id, "model_version": self.model_version, "embedding": vector}
                for title, vector in zip(batch, vectors, strict=True)
            ]
            # ON CONFLICT DO NOTHING: a concurrent read-path top-up can embed the same titles
            # between our SELECT and this INSERT (both lazy passes snapshot the same "missing" set).
            # Converge silently instead of raising a duplicate-key IntegrityError that poisons the
            # transaction. The vector is identical either way, so the race's loser loses nothing.
            self.session.execute(
                pg_insert(TitleEmbedding)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["title_id", "model_version"])
            )
            self.session.flush()
        logger.info(
            "embeddings.done",
            extra={"embedded_count": len(titles), "model_version": self.model_version},
        )
        return len(titles)
