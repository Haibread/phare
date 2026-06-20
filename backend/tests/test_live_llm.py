"""Live smoke tests against the configured LLM provider.

These make real network calls (and cost a few fractions of a cent), so they are **opt-in**: they
only run when ``PHARE_LIVE_LLM=1`` is set, and skip cleanly without an ``LLM_API_KEY``. They never
run in CI or on a normal ``pytest`` invocation. Use them to verify a real provider config end to
end — that the chat/agent models answer and the embedding model returns vectors that fit the schema.

    PHARE_LIVE_LLM=1 uv run pytest tests/test_live_llm.py -q
"""

from __future__ import annotations

import os

import pytest

from phare.core.config import get_settings
from phare.embeddings.version import get_embedding_provider
from phare.providers.llm import OpenAILLMProvider

pytestmark = pytest.mark.skipif(
    not os.environ.get("PHARE_LIVE_LLM"),
    reason="set PHARE_LIVE_LLM=1 to run live provider smoke tests",
)


def _settings():
    settings = get_settings()
    if not settings.llm_api_key:
        pytest.skip("no LLM_API_KEY configured")
    return settings


def _chat_provider(model: str) -> OpenAILLMProvider:
    settings = get_settings()
    return OpenAILLMProvider(
        api_key=settings.llm_api_key or "",
        chat_model=model,
        embedding_model=settings.llm_embedding_model,
        base_url=settings.llm_base_url,
    )


def test_chat_model_responds() -> None:
    settings = _settings()
    reply = _chat_provider(settings.llm_chat_model).complete("Reply with the single word: pong")
    assert reply.strip()  # the workhorse model is reachable and answers


def test_agent_model_responds() -> None:
    settings = _settings()
    reply = _chat_provider(settings.agent_chat_model).complete("Reply with the single word: pong")
    assert reply.strip()  # the (possibly bigger) agent model is reachable on this endpoint


def test_embedding_returns_configured_dimension() -> None:
    settings = _settings()
    provider = get_embedding_provider(settings)
    [vector] = provider.embed(["a quiet sci-fi about memory and loss"])
    # With LLM_EMBEDDING_REQUEST_DIMENSIONS=true the model must return schema-sized vectors so they
    # fit Vector(EMBEDDING_DIM) without a migration.
    assert len(vector) == settings.llm_embedding_dim
    assert any(abs(x) > 0 for x in vector)  # a real embedding, not all-zeros
