"""Optional LLM-as-judge: a guardrail, never the grade. Skipped entirely without a key.

The cheap model flags fit problems, spoiler leaks, and cross-user references. It's noisy, so it
only ever surfaces warnings — it never gates CI on its own.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from phare.providers.types import LLMProvider
from phare.recommend.schema import Recommendation

logger = logging.getLogger(__name__)

_PROMPT = """You audit movie/TV recommendation explanations for a single viewer.

Flag ONLY these problems, as a JSON array of short strings (empty array if none):
- a plot spoiler for the title
- a reference to another person/user ("because Bob liked it")
- an explanation that contradicts the stated genres

Explanations:
"""


def judge_explanations(llm: LLMProvider, recommendations: Sequence[Recommendation]) -> list[str]:
    """Return a list of flagged issues. Best-effort: returns [] on any provider/parse failure."""
    lines = [
        f"- {rec.title} ({', '.join(rec.genres)}): {rec.explanation or ''}"
        for rec in recommendations
    ]
    try:
        raw = llm.complete(_PROMPT + "\n".join(lines), max_tokens=400).strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1].removeprefix("json").strip()
        flags = json.loads(raw)
        return [str(flag) for flag in flags] if isinstance(flags, list) else []
    except Exception:  # noqa: BLE001 - the judge is advisory; never let it fail the run
        logger.warning("eval.judge_failed")
        return []
