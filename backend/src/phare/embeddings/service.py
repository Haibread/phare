"""Title embedding pipeline.

Builds the embedding input from a title's metadata, embeds via the LLM provider, and stores
the vector stamped with the embedding model version. Titles missing an embedding for the
current model version are (re-)embedded; this is how a model change triggers a re-embed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

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

    def _titles_missing_embedding(self) -> Sequence[Title]:
        embedded = (
            select(TitleEmbedding.title_id)
            .where(TitleEmbedding.model_version == self.model_version)
            .scalar_subquery()
        )
        return self.session.scalars(select(Title).where(Title.id.notin_(embedded))).all()

    def embed_missing(self, batch_size: int = 64) -> int:
        """Embed every title lacking a vector for the current model version. Returns count."""
        titles = list(self._titles_missing_embedding())
        for start in range(0, len(titles), batch_size):
            batch = titles[start : start + batch_size]
            vectors = self.llm.embed([build_embedding_text(t) for t in batch])
            for title, vector in zip(batch, vectors, strict=True):
                self.session.add(
                    TitleEmbedding(
                        title_id=title.id,
                        embedding=vector,
                        model_version=self.model_version,
                    )
                )
            self.session.flush()
        logger.info(
            "embeddings.done",
            extra={"embedded_count": len(titles), "model_version": self.model_version},
        )
        return len(titles)
