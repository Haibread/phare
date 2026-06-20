"""Shared FastAPI dependencies for the engine endpoints.

Two distinct LLM concerns (see ``recommend/explain`` and ``agent/intent``):
- **embeddings** always resolve to a provider (local hash fallback when no key) so retrieval
  works offline — overridable in tests;
- **chat completion** is *optional*: ``None`` when unconfigured, and the engine degrades to
  templated explanations / keyword intent.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from phare.core.config import get_settings
from phare.core.net import validate_external_url
from phare.embeddings.version import embedding_model_version, get_embedding_provider
from phare.providers.llm import OpenAILLMProvider
from phare.providers.types import LLMProvider


def require_safe_url(url: str) -> str:
    """SSRF guard for user-supplied ``base_url``s; 400 if it isn't safe to fetch server-side."""
    try:
        return validate_external_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@dataclass(frozen=True)
class Embedder:
    """An embedding provider paired with the model version its vectors are stamped with."""

    provider: LLMProvider
    model_version: str


def get_embedder() -> Embedder:
    """Embedding provider + matching version. Local hash fallback when no LLM key is set."""
    settings = get_settings()
    return Embedder(
        provider=get_embedding_provider(settings),
        model_version=embedding_model_version(settings),
    )


def get_optional_chat_llm() -> LLMProvider | None:
    """Chat-completion provider for explanations / intent / replies, or ``None`` if unconfigured."""
    settings = get_settings()
    if not settings.llm_api_key:
        return None
    return OpenAILLMProvider(
        api_key=settings.llm_api_key,
        chat_model=settings.llm_chat_model,
        embedding_model=settings.llm_embedding_model,
        base_url=settings.llm_base_url,
    )
