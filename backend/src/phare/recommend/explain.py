"""Explanations: short, spoiler-safe, confidence-aware. LLM when available, else a template.

Hard rules (docs/design.md): describe *appeal* (tone, themes, fit), never plot events of an
unwatched title; express confidence; never cite another user. The template fallback keeps the
whole pipeline working offline; it only ever uses structured metadata (genres, year), never the
free-text overview, so it cannot leak plot.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from phare.providers.types import LLMProvider
from phare.recommend.schema import Recommendation

logger = logging.getLogger(__name__)

_SYSTEM = """You write one-sentence, spoiler-safe reasons a viewer might enjoy a title.

Rules:
- Describe appeal only: tone, themes, mood, why it fits their taste. NEVER mention plot events.
- Never mention other people or users.
- Be honest about confidence; for a "discovery pick" frame it as a stretch worth trying.
- Output ONLY the sentence, no preamble.
"""


def _template(rec: Recommendation, taste: Mapping[str, Any]) -> str:
    """Deterministic, metadata-only explanation. Spoiler-proof by construction."""
    genres = ", ".join(rec.genres[:2]) if rec.genres else "genre-spanning"
    era = f" from {rec.year}" if rec.year else ""
    if rec.is_swing:
        return (
            f"A {genres} {rec.kind}{era} — a discovery pick outside your usual lane, "
            f"offered as a deliberate stretch rather than a sure thing."
        )
    affinities = taste.get("affinities") or {}
    matched = next(
        (key for key in affinities if key.lower() in {g.lower() for g in rec.genres}), None
    )
    because = f" that leans into your taste for {matched}" if matched else " that fits your profile"
    return f"A {genres} {rec.kind}{era}{because}."


def _llm_prompt(rec: Recommendation, taste: Mapping[str, Any]) -> str:
    summary = taste.get("summary") or "(no taste summary yet)"
    genres = ", ".join(rec.genres) or "unknown"
    kind = "discovery pick (a stretch)" if rec.is_swing else "a strong match"
    return (
        f"{_SYSTEM}\nViewer taste: {summary}\n"
        f"Title: {rec.title} ({rec.year or 'n/a'}) — genres: {genres}\n"
        f"This is {kind}. Write the sentence."
    )


def explain(
    recommendations: Sequence[Recommendation],
    taste: Mapping[str, Any],
    llm: LLMProvider | None,
) -> list[Recommendation]:
    """Attach an explanation to each recommendation, in place-returning a new list."""
    out: list[Recommendation] = []
    for rec in recommendations:
        text: str | None = None
        if llm is not None:
            try:
                text = llm.complete(_llm_prompt(rec, taste)).strip() or None
            except Exception:  # noqa: BLE001 - never let a flaky LLM sink the whole row
                logger.warning("recommend.explain_failed", extra={"title": rec.title})
        if text is None:
            text = _template(rec, taste)
        out.append(rec.model_copy(update={"explanation": text}))
    return out
