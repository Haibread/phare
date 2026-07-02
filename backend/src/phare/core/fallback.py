"""One place every graceful degradation announces itself (review G1).

A fallback that stays silent turns a real failure — a vocabulary miss, a provider blip, a truncated
backfill — into indistinguishable "it answered but it's mediocre", with no trace for the operator.
Every degrade-and-continue path routes through :func:`record_fallback`, so each one emits a
structured warning *and* increments a single OpenTelemetry counter tagged by ``component`` +
``reason``. The metric is a no-op until an OTLP endpoint is configured (see ``core/telemetry.py``);
the log always fires. See ``docs/observability.md`` for how to read it.
"""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry import metrics

logger = logging.getLogger("phare.fallback")

_meter = metrics.get_meter("phare.fallback")
_fallback_counter = _meter.create_counter(
    "phare.fallback",
    unit="1",
    description="A component fell back to a degraded path (attributes: component, reason).",
)


def record_fallback(component: str, reason: str, **fields: Any) -> None:
    """Announce a graceful degradation: a structured warning + a ``phare.fallback`` metric bump
    tagged ``{component, reason}``. ``fields`` carry extra log context (wanted genres, title, …).

    ``component`` names the brick that degraded (``genre_filter``, ``explain``, ``planner`` …);
    ``reason`` is the machine-readable cause (``no_match``, ``llm_error`` …) — keep both stable,
    they become metric label values.
    """
    logger.warning(f"{component}.fallback", extra={"reason": reason, **fields})
    _fallback_counter.add(1, {"component": component, "reason": reason})
