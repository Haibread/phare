"""Local hash embedding provider: deterministic, correctly sized, discriminative."""

from __future__ import annotations

from phare.core.config import Settings
from phare.embeddings.version import embedding_model_version, get_embedding_provider
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider


def test_deterministic_and_sized() -> None:
    provider = LocalHashEmbeddingProvider(dim=1536)
    first = provider.embed(["Dune", "Severance"])
    second = provider.embed(["Dune", "Severance"])

    assert first == second  # deterministic
    assert all(len(v) == 1536 for v in first)
    assert all(0.0 <= x <= 1.0 for v in first for x in v)


def test_distinct_text_distinct_vector() -> None:
    [a, b] = LocalHashEmbeddingProvider(dim=64).embed(["comedy", "horror"])
    assert a != b
    # Not a single tiled block: later dims differ from the first 32 (re-hash per block).
    assert a[:32] != a[32:64]


def test_version_and_provider_follow_llm_key() -> None:
    local = Settings(llm_api_key=None)
    assert embedding_model_version(local) == LOCAL_MODEL_VERSION
    assert isinstance(get_embedding_provider(local), LocalHashEmbeddingProvider)

    keyed = Settings(llm_api_key="sk-test", llm_embedding_model="text-embedding-3-small")
    assert embedding_model_version(keyed) == "text-embedding-3-small"
