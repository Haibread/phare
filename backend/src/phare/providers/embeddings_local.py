"""Local, deterministic embedding provider — no API key, no network.

The recommendation engine's deterministic core (vector retrieval + re-ranker) must run with
zero external credentials so CI/E2E exercise the *real* pipeline, and so the app degrades
gracefully onto a weak local model (design principle 5). This provider hashes text into a
stable pseudo-embedding. It is good enough for relative similarity within a single space; it
is **not** semantically meaningful, so it lives behind its own model version and is never mixed
with a real embedding model's vectors.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence

logger = logging.getLogger(__name__)

# Model-version tag for vectors produced here. Stamped onto every TitleEmbedding so the local
# space is never queried with (or polluted by) a real embedding model's vectors.
LOCAL_MODEL_VERSION = "local-hash-v1"


class LocalHashEmbeddingProvider:
    """Deterministic hash-based embeddings. Satisfies the ``embed`` half of ``LLMProvider``."""

    name = "local-hash"

    def __init__(self, dim: int = 1536) -> None:
        self.dim = dim

    def complete(self, prompt: str) -> str:  # pragma: no cover - not a chat model
        raise NotImplementedError("LocalHashEmbeddingProvider does not do chat completion")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        logger.debug("embeddings.local", extra={"count": len(texts), "dim": self.dim})
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        # Expand the digest deterministically to ``dim`` floats in [0, 1). Re-hash per block so
        # the vector isn't just a short pattern tiled across 1536 dims (that would collapse
        # cosine similarity). Normalisation is left to pgvector's cosine distance.
        out: list[float] = []
        block = 0
        while len(out) < self.dim:
            digest = hashlib.sha256(f"{text}\x00{block}".encode()).digest()
            out.extend(byte / 255.0 for byte in digest)
            block += 1
        return out[: self.dim]
