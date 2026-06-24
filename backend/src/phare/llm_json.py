"""Tolerant JSON extraction from LLM completions.

Workhorse models don't always hand back clean JSON: a reasoning model wraps its answer in
``<think>…</think>`` (and can spend its whole token budget thinking, leaving the answer empty or
truncated), and plenty of models fence JSON in markdown or wrap it in a sentence of prose. The
planner, taste extraction, and themed-row namer all need a structured value back, so they share one
parser instead of each re-implementing fence stripping — and gain reasoning-model tolerance in a
single place.
"""

from __future__ import annotations

import json
import re
from typing import Any

# A reasoning block emitted before the actual answer. Non-greedy so several blocks each get
# stripped; DOTALL so a block can span lines.
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# The outermost JSON object or array — used to salvage a value a model wrapped in prose.
_JSON_SPAN = re.compile(r"[\[{].*[\]}]", re.DOTALL)


def strip_reasoning(raw: str | None) -> str:
    """Drop ``<think>…</think>`` reasoning blocks (and surrounding whitespace) from a completion."""
    return _THINK.sub("", raw or "").strip()


def extract_json(raw: str | None) -> Any:
    """Parse a JSON value from an LLM completion.

    Tolerates ``None``/empty completions, ``<think>…</think>`` reasoning blocks, markdown code
    fences, and prose surrounding the JSON. Raises :class:`ValueError` (which
    ``json.JSONDecodeError`` subclasses) when nothing parseable is left — callers decide whether
    that means a graceful fallback or a surfaced error.
    """
    text = strip_reasoning(raw)
    if text.startswith("```"):
        text = text.split("```", 2)[1].removeprefix("json").strip()
    if not text:
        raise ValueError("empty LLM completion")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        span = _JSON_SPAN.search(text)
        if span is None:
            raise
        return json.loads(span.group(0))
