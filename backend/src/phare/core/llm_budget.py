"""LLM token metering + a spend ceiling (review I2).

The API returns ``usage`` on every response and nobody read it — no visibility into the bill and no
guard against it. This module:

- emits a ``phare.llm.tokens`` OpenTelemetry counter tagged ``{model, endpoint, direction}`` plus a
  structured debug log on every call, so token spend is observable; and
- tracks cumulative output tokens for the current calendar month in-process and, when
  ``LLM_MONTHLY_TOKEN_BUDGET`` is set, trips a circuit breaker: further **mechanical** LLM calls
  (``complete``/``embed`` — taste, explanations, planning, embeddings, the bulk of the spend) raise
  :class:`LLMBudgetExceeded`, which the existing fallbacks (templates, degraded plan, deterministic
  taste) already turn into a graceful degrade. The single rate-limited chat *reply* stream is left
  ungated.

The counter is process-global (the app is single-process). Per-user attribution would need user
context threaded through the provider interface, which is deliberately user-agnostic — deferred.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from opentelemetry import metrics

from phare.core.fallback import record_fallback

logger = logging.getLogger("phare.llm")

_meter = metrics.get_meter("phare.llm")
_token_counter = _meter.create_counter(
    "phare.llm.tokens",
    unit="1",
    description="LLM tokens used (attributes: model, endpoint, direction=in|out).",
)


class LLMBudgetExceeded(RuntimeError):
    """Raised when a mechanical LLM call is refused because the monthly token budget is spent."""


class _MonthlySpend:
    """Cumulative output tokens for the current calendar month, reset when the month rolls over."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._month: tuple[int, int] | None = None
        self._tokens = 0

    def _roll(self, month: tuple[int, int]) -> None:
        if self._month != month:
            self._month = month
            self._tokens = 0

    def add(self, tokens: int, month: tuple[int, int]) -> None:
        with self._lock:
            self._roll(month)
            self._tokens += tokens

    def spent(self, month: tuple[int, int]) -> int:
        with self._lock:
            self._roll(month)
            return self._tokens


_spend = _MonthlySpend()


def _current_month() -> tuple[int, int]:
    # Imported lazily: bare datetime.now() is fine in the app (only workflow scripts forbid it).
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return (now.year, now.month)


def record_usage(model: str, endpoint: str, usage: Any) -> None:
    """Emit the token metric + a debug log and accrue output tokens toward the monthly budget.

    ``usage`` is the OpenAI-compatible ``usage`` object (``prompt_tokens`` / ``completion_tokens`` /
    ``total_tokens``); tolerated as absent/partial so a non-conforming provider never breaks a call.
    """
    if not isinstance(usage, dict):
        return
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    if prompt:
        _token_counter.add(prompt, {"model": model, "endpoint": endpoint, "direction": "in"})
    if completion:
        _token_counter.add(completion, {"model": model, "endpoint": endpoint, "direction": "out"})
    logger.debug(
        "llm.usage",
        extra={"model": model, "endpoint": endpoint, "prompt": prompt, "completion": completion},
    )
    # Bill the total against the monthly ceiling (input tokens cost too).
    _spend.add(total, _current_month())


def over_budget(monthly_budget: int) -> bool:
    """True when a positive monthly budget has been reached for the current month."""
    if monthly_budget <= 0:
        return False
    return _spend.spent(_current_month()) >= monthly_budget


def ensure_budget(monthly_budget: int, endpoint: str) -> None:
    """Raise :class:`LLMBudgetExceeded` (and signal a fallback) if the monthly budget is spent."""
    if over_budget(monthly_budget):
        record_fallback("llm_budget", "exhausted", endpoint=endpoint)
        raise LLMBudgetExceeded(f"Monthly LLM token budget reached before {endpoint} call")


def _reset_for_tests() -> None:
    """Test hook: clear the accrued monthly spend."""
    with _spend._lock:  # noqa: SLF001 - test-only reset of the module singleton
        _spend._month = None
        _spend._tokens = 0
