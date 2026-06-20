"""OpenAI-compatible LLM provider (chat completions + embeddings).

Works against OpenAI, OpenRouter, or any compatible server (incl. local). The default
provider for taste extraction, explanations, and the chat agent. Swappable via config.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from phare.providers.http import DEFAULT_MAX_RETRIES, request_with_retry

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
        embedding_dimensions: int | None = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._chat_model = chat_model
        self._embedding_model = embedding_model
        # When set, request this output size via the standard OpenAI `dimensions` parameter — for
        # models with configurable (Matryoshka) embeddings, so they fit the schema without a
        # re-embed. Left unset for models that don't accept the parameter.
        self._embedding_dimensions = embedding_dimensions
        self._max_retries = max_retries
        self._sleep = sleep
        self._client = client or httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )

    def _post(self, path: str, payload: dict[str, object]) -> Any:
        # Hosted LLM endpoints rate-limit on 429 (often with Retry-After); back off and retry.
        response = request_with_retry(
            self._client,
            "POST",
            path,
            name="llm",
            max_retries=self._max_retries,
            sleep=self._sleep,
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def complete(self, prompt: str) -> str:
        logger.debug("llm.complete", extra={"model": self._chat_model})
        data = self._post(
            "/chat/completions",
            {"model": self._chat_model, "messages": [{"role": "user", "content": prompt}]},
        )
        return data["choices"][0]["message"]["content"]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        logger.debug("llm.embed", extra={"model": self._embedding_model, "count": len(texts)})
        payload: dict[str, object] = {"model": self._embedding_model, "input": list(texts)}
        if self._embedding_dimensions is not None:
            payload["dimensions"] = self._embedding_dimensions
        data = sorted(self._post("/embeddings", payload)["data"], key=lambda d: d["index"])
        return [item["embedding"] for item in data]
