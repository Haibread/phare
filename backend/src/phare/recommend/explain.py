"""Explanations: short, spoiler-safe, confidence-aware. LLM when available, else a template.

Hard rules (docs/design.md): describe *appeal* (tone, themes, fit), never plot events of an
unwatched title; express confidence; never cite another user. The template fallback keeps the
whole pipeline working offline; it only ever uses structured metadata (genres, year), never the
free-text overview, so it cannot leak plot.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from phare.providers.types import LLMProvider
from phare.recommend.schema import Recommendation

logger = logging.getLogger(__name__)

# Inputs to the LLM are already spoiler-safe (we never pass the overview), but the *output* is the
# model's; a weak/jailbroken model could still narrate plot. This is a cheap last line of defence,
# not a guarantee: a one-line appeal blurb has no business being long or naming a plot reveal.
_MAX_EXPLANATION_LEN = 320
_SPOILER_MARKERS = re.compile(
    r"\b(turns out|plot twist|the twist|is revealed|revealed to be|the killer|the murderer|"
    r"dies|is killed|killed off|betrays|the ending|ending reveals|spoiler)\b",
    re.IGNORECASE,
)


def is_spoiler_safe(text: str) -> bool:
    """Reject an explanation that's overly long or names a plot reveal. Heuristic, not proof."""
    if len(text) > _MAX_EXPLANATION_LEN:
        return False
    return _SPOILER_MARKERS.search(text) is None


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
                candidate = llm.complete(_llm_prompt(rec, taste)).strip()
                if candidate and is_spoiler_safe(candidate):
                    text = candidate
                elif candidate:
                    # Output tripped the spoiler guard — drop it and fall back to the safe template.
                    logger.warning("recommend.explain_rejected", extra={"title": rec.title})
            except Exception:  # noqa: BLE001 - never let a flaky LLM sink the whole row
                logger.warning("recommend.explain_failed", extra={"title": rec.title})
        if text is None:
            text = _template(rec, taste)
        out.append(rec.model_copy(update={"explanation": text}))
    return out
