"""Single source of truth for the active embedding model version + provider selection.

Every embedding vector is stamped with a model version (the ``TitleEmbedding`` composite PK).
Retrieval MUST query the same version it embedded with, or it queries an empty / mismatched
space. Centralising the choice here guarantees the embed step and the query agree.
"""

from __future__ import annotations

from phare.core.config import Settings
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.llm import OpenAILLMProvider
from phare.providers.types import LLMProvider


def embedding_model_version(settings: Settings) -> str:
    """Active embedding space tag: the real model when configured, else the local one."""
    if settings.llm_api_key:
        return settings.llm_embedding_model
    return LOCAL_MODEL_VERSION


def get_embedding_provider(settings: Settings) -> LLMProvider:
    """Embedding provider matching :func:`embedding_model_version` — OpenAI if keyed, else local."""
    if settings.llm_api_key:
        return OpenAILLMProvider(
            api_key=settings.llm_api_key,
            chat_model=settings.llm_chat_model,
            embedding_model=settings.llm_embedding_model,
            base_url=settings.llm_base_url,
            monthly_token_budget=settings.llm_monthly_token_budget,
            embedding_dimensions=(
                settings.llm_embedding_dim if settings.llm_embedding_request_dimensions else None
            ),
            timeout=settings.llm_timeout_seconds,
        )
    return LocalHashEmbeddingProvider(dim=settings.llm_embedding_dim)
