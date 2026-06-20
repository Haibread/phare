"""Explanations: short, spoiler-safe, confidence-aware. LLM when available, else a template.

Hard rules (docs/design.md): describe *appeal* (tone, themes, fit), never plot events of an
unwatched title; express confidence; never cite another user. The template fallback keeps the
whole pipeline working offline; it only ever uses structured metadata (genres, year), never the
free-text overview, so it cannot leak plot.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from phare.providers.http import TTLCache
from phare.providers.types import LLMProvider
from phare.recommend.schema import Recommendation

logger = logging.getLogger(__name__)

# Explanation calls are slow, blocking HTTP — a real provider is seconds per call. Within one
# render the bounded set runs concurrently (it's I/O), so wall-time is ~one call, not their sum.
_MAX_EXPLAIN_WORKERS = 8

# Process-wide cache of accepted LLM explanations, keyed by (title, swing-ness, taste fingerprint),
# so a blurb is generated once and reused across rows and across page loads — not re-computed on
# every home render. Keyed on the taste summary, so it self-invalidates when taste changes. A long
# TTL is fine; the key carries correctness, the TTL only bounds memory.
_EXPLANATION_CACHE = TTLCache(ttl=86_400, maxsize=8192)


def _taste_fingerprint(taste: Mapping[str, Any]) -> str:
    return hashlib.sha256(str(taste.get("summary") or "").encode()).hexdigest()[:16]


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


def coerce_safe(text: str) -> str | None:
    """Best-effort spoiler-safe blurb, or ``None`` to reject.

    A genuine plot-reveal marker rejects outright. But the prompt asks for *one sentence*, and a
    verbose model sometimes runs past the length cap with extra, harmless clauses — discarding that
    (and caching the template forever) is why on-taste top picks intermittently lost their blurb.
    So an over-long but marker-free reply is trimmed to its first sentence rather than thrown away.
    """
    if _SPOILER_MARKERS.search(text):
        return None
    if len(text) <= _MAX_EXPLANATION_LEN:
        return text
    first = re.match(r"\s*(.+?[.!?])(\s|$)", text, re.S)
    sentence = first.group(1).strip() if first else ""
    return sentence if sentence and len(sentence) <= _MAX_EXPLANATION_LEN else None


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


# A budget high enough to never bind in practice — the standalone/back-compat path is unbounded.
_UNBOUNDED = 1_000_000_000


@dataclass
class Explainer:
    """Attaches explanations, bounding LLM spend per render.

    Each *uncached* LLM call consumes ``budget``; once it's spent, the rest fall back to the
    deterministic template (and earlier blurbs are cached for the next render). This is what keeps
    a home page — which explains dozens of items across rows — from making dozens of slow,
    sequential calls to a real provider. ``llm=None`` templates everything (offline / chat path).
    """

    llm: LLMProvider | None
    cache: TTLCache | None = None
    budget: int = _UNBOUNDED

    def explain(
        self, recommendations: Sequence[Recommendation], taste: Mapping[str, Any]
    ) -> list[Recommendation]:
        """Attach an explanation to each recommendation, returning a new list.

        Cached/templated items resolve inline; the uncached LLM calls (bounded by ``budget``) are
        decided sequentially — so spend is deterministic and front-loaded onto the top-ranked
        items — then fired concurrently.
        """
        fingerprint = _taste_fingerprint(taste)
        texts: list[str | None] = [None] * len(recommendations)
        to_generate: list[tuple[int, Recommendation, tuple[str, bool, str]]] = []
        for i, rec in enumerate(recommendations):
            if self.llm is None:
                texts[i] = _template(rec, taste)
                continue
            key = (str(rec.title_id), bool(rec.is_swing), fingerprint)
            if self.cache is not None and (cached := self.cache.get(key)) is not None:
                texts[i] = cached
            elif self.budget > 0:
                self.budget -= 1  # bound *calls*, whether or not the output is accepted
                to_generate.append((i, rec, key))
            else:
                texts[i] = _template(rec, taste)

        if to_generate:
            workers = min(len(to_generate), _MAX_EXPLAIN_WORKERS)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for i, text in pool.map(
                    lambda job: (job[0], self._generate(job[1], taste, job[2])), to_generate
                ):
                    texts[i] = text

        return [
            rec.model_copy(update={"explanation": texts[i]})
            for i, rec in enumerate(recommendations)
        ]

    def _generate(
        self, rec: Recommendation, taste: Mapping[str, Any], key: tuple[str, bool, str]
    ) -> str:
        """One live LLM explanation with the spoiler post-check, falling back to the template.

        The *outcome* is cached either way (blurb or template), so a title is attempted at most once
        per taste version. Without this, a model whose output keeps tripping the spoiler guard would
        re-burn the budget on every render and the cache would never warm.
        """
        text = self._call_or_template(rec, taste)
        if self.cache is not None:
            self.cache.set(key, text)
        return text

    def _call_or_template(self, rec: Recommendation, taste: Mapping[str, Any]) -> str:
        try:
            candidate = self.llm.complete(_llm_prompt(rec, taste)).strip()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - never let a flaky LLM sink the whole row
            logger.warning("recommend.explain_failed", extra={"title": rec.title})
            return _template(rec, taste)
        safe = coerce_safe(candidate) if candidate else None
        if safe:
            return safe
        if candidate:
            # A genuine plot-reveal marker — drop it and fall back to the safe template.
            logger.warning("recommend.explain_rejected", extra={"title": rec.title})
        return _template(rec, taste)


def explain(
    recommendations: Sequence[Recommendation],
    taste: Mapping[str, Any],
    llm: LLMProvider | None,
) -> list[Recommendation]:
    """Explain each recommendation. Back-compat shim: unbounded, uncached (per-call behavior)."""
    return Explainer(llm=llm).explain(recommendations, taste)


def lazy_reason(
    rec: Recommendation, taste: Mapping[str, Any], llm: LLMProvider | None, cache: TTLCache | None
) -> str:
    """A "why this fits you" reason for the detail sheet — generated on demand, cached on success.

    Looser than the card-label blurb: a detail view can hold a short paragraph, so this only
    rejects on a genuine plot-reveal *marker* (not length). Falls back to the template offline or
    on a marker/error. Successes are cached per (title, taste); a (rare) rejection isn't, so a
    re-open retries.
    """
    key = ("reason", str(rec.title_id), _taste_fingerprint(taste))
    if cache is not None and (hit := cache.get(key)) is not None:
        return str(hit)
    if llm is None:
        return _template(rec, taste)
    try:
        text = llm.complete(_llm_prompt(rec, taste)).strip()
    except Exception:  # noqa: BLE001 - a flaky model must not sink the request
        logger.warning("recommend.reason_failed", extra={"title": rec.title})
        return _template(rec, taste)
    if text and _SPOILER_MARKERS.search(text) is None:
        if cache is not None:
            cache.set(key, text)
        return text
    if text:
        logger.warning("recommend.reason_rejected", extra={"title": rec.title})
    return _template(rec, taste)
