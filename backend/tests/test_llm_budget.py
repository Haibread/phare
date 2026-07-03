"""LLM token metering + the monthly spend circuit breaker (review I2)."""

from __future__ import annotations

import httpx
import pytest

from phare.core import llm_budget
from phare.core.llm_budget import LLMBudgetExceeded, ensure_budget, over_budget, record_usage
from phare.providers.llm import OpenAILLMProvider


def test_record_usage_accrues_and_budget_trips() -> None:
    llm_budget._reset_for_tests()
    try:
        assert over_budget(0) is False  # 0 = unlimited
        record_usage("m", "chat", {"prompt_tokens": 100, "completion_tokens": 50})
        assert over_budget(1000) is False
        assert over_budget(150) is True  # 100 + 50 reached the ceiling
        ensure_budget(0, "chat")  # unlimited → no raise
        with pytest.raises(LLMBudgetExceeded):
            ensure_budget(150, "chat")
    finally:
        llm_budget._reset_for_tests()


def test_record_usage_tolerates_missing_usage() -> None:
    llm_budget._reset_for_tests()
    try:
        record_usage("m", "chat", None)  # non-conforming provider → no crash, nothing accrued
        record_usage("m", "chat", {})
        assert over_budget(1) is False
    finally:
        llm_budget._reset_for_tests()


def test_provider_meters_usage_and_enforces_the_budget() -> None:
    llm_budget._reset_for_tests()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": 200}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://llm.test")
    provider = OpenAILLMProvider(
        api_key="k",
        chat_model="m",
        embedding_model="e",
        client=client,
        monthly_token_budget=150,
    )
    try:
        # The first call goes through and records its 200 tokens.
        assert provider.complete("hey") == "hi"
        # That already exceeds the 150-token monthly budget, so the next mechanical call is refused
        # (its callers' deterministic fallbacks then take over).
        with pytest.raises(LLMBudgetExceeded):
            provider.complete("again")
    finally:
        llm_budget._reset_for_tests()
