"""OpenAI-compatible provider: reasoning headroom + null-content handling (HTTP via mock)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from phare.providers.llm import OpenAILLMProvider


def _provider(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any
) -> OpenAILLMProvider:
    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    return OpenAILLMProvider(
        api_key="k", chat_model="m", embedding_model="e", client=client, **kwargs
    )


def _content_handler(content: object) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        handler.payload = json.loads(request.content)  # type: ignore[attr-defined]
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


def test_complete_adds_reasoning_headroom_to_max_tokens() -> None:
    handler = _content_handler("hi")
    provider = _provider(handler, reasoning_headroom=2000)
    assert provider.complete("x", max_tokens=300) == "hi"
    assert handler.payload["max_tokens"] == 2300  # type: ignore[attr-defined]


def test_complete_no_headroom_by_default() -> None:
    handler = _content_handler("hi")
    assert _provider(handler).complete("x", max_tokens=300) == "hi"
    assert handler.payload["max_tokens"] == 300  # type: ignore[attr-defined]


def test_complete_coerces_null_content_to_empty_string() -> None:
    # A reasoning model that burns its budget thinking returns content: null — must not crash the
    # `-> str` contract (this is what 500'd taste generation before the guard).
    assert _provider(_content_handler(None)).complete("x") == ""
