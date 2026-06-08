"""OpenAI-compatible LLM provider (chat completions + embeddings).

Works against OpenAI, OpenRouter, or any compatible server (incl. local). The default
provider for taste extraction, explanations, and the chat agent. Swappable via config.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

logger = logging.getLogger(__name__)


class OpenAILLMProvider:
    """LLMProvider backed by an OpenAI-compatible HTTP API."""

    name = "openai-compatible"

    def __init__(
        self,
        api_key: str,
        chat_model: str,
        embedding_model: str,
        base_url: str = "https://api.openai.com/v1",
        client: httpx.Client | None = None,
    ) -> None:
        self._chat_model = chat_model
        self._embedding_model = embedding_model
        self._client = client or httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )

    def complete(self, prompt: str) -> str:
        logger.debug("llm.complete", extra={"model": self._chat_model})
        response = self._client.post(
            "/chat/completions",
            json={
                "model": self._chat_model,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        logger.debug("llm.embed", extra={"model": self._embedding_model, "count": len(texts)})
        response = self._client.post(
            "/embeddings",
            json={"model": self._embedding_model, "input": list(texts)},
        )
        response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda d: d["index"])
        return [item["embedding"] for item in data]
