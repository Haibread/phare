"""Shared FastAPI dependencies for the engine endpoints.

Two distinct LLM concerns (see ``recommend/explain`` and ``agent/intent``):
- **embeddings** always resolve to a provider (local hash fallback when no key) so retrieval
  works offline — overridable in tests;
- **chat completion** is *optional*: ``None`` when unconfigured, and the engine degrades to
  templated explanations / keyword intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from phare.core.config import get_settings
from phare.core.i18n import Language, parse_accept_language
from phare.core.net import validate_external_url
from phare.embeddings.version import embedding_model_version, get_embedding_provider
from phare.providers.llm import OpenAILLMProvider
from phare.providers.tmdb import TMDBMetadataProvider
from phare.providers.types import LLMProvider, MetadataProvider


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


def get_language(accept_language: Annotated[str | None, Header()] = None) -> Language:
    """Resolve the request language from ``Accept-Language``, else the configured default.

    Drives both TMDB's ``language`` param and which side of the localized catalog / LLM prompt is
    used, so backend-generated text matches the language the UI is showing."""
    return parse_accept_language(accept_language, default=get_settings().default_language)


def get_embedder() -> Embedder:
    """Embedding provider + matching version. Local hash fallback when no LLM key is set."""
    settings = get_settings()
    return Embedder(
        provider=get_embedding_provider(settings),
        model_version=embedding_model_version(settings),
    )


def get_optional_metadata_provider(
    language: Annotated[Language, Depends(get_language)],
) -> MetadataProvider | None:
    """TMDB metadata provider bound to the request language, or ``None`` when no TMDB key is set.

    Injected (rather than built inline) so the title-detail cache fill is testable — a test swaps
    in a counting/failing provider to prove the second open skips TMDB and that an outage is
    absorbed by the stored copy."""
    settings = get_settings()
    if not settings.tmdb_api_key:
        return None
    return TMDBMetadataProvider(
        api_key=settings.tmdb_api_key,
        base_url=settings.tmdb_base_url,
        language=language,
        cache_ttl=settings.tmdb_cache_ttl_seconds,
    )


def get_optional_chat_llm() -> LLMProvider | None:
    """Workhorse chat-completion provider (explanations / taste / intent), or ``None`` if no key."""
    settings = get_settings()
    if not settings.llm_api_key:
        return None
    return OpenAILLMProvider(
        api_key=settings.llm_api_key,
        chat_model=settings.llm_chat_model,
        embedding_model=settings.llm_embedding_model,
        base_url=settings.llm_base_url,
        reasoning_headroom=settings.reasoning_headroom,
    )


def get_optional_agent_llm() -> LLMProvider | None:
    """Conversational chat-agent provider (planner + natural-language reply). May be a bigger model
    than the workhorse (``LLM_AGENT_MODEL``); falls back to the chat model. ``None`` if no key."""
    settings = get_settings()
    if not settings.llm_api_key:
        return None
    return OpenAILLMProvider(
        api_key=settings.llm_api_key,
        chat_model=settings.agent_chat_model,
        embedding_model=settings.llm_embedding_model,
        base_url=settings.llm_base_url,
        reasoning_headroom=settings.reasoning_headroom,
    )
