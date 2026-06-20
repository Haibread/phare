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
import uuid
from collections.abc import Hashable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from phare.db.models import TitleExplanation
from phare.providers.http import TTLCache
from phare.providers.types import LLMProvider, stream_text
from phare.recommend.schema import Recommendation

logger = logging.getLogger(__name__)


class ReasonCache(Protocol):
    """The slice of cache behaviour the reason path needs — satisfied by both the in-process
    :class:`TTLCache` and the DB-backed :class:`PersistentReasonCache`."""

    def get(self, key: Hashable) -> Any | None: ...

    def set(self, key: Hashable, value: str) -> None: ...


# Explanation calls are slow, blocking HTTP — a real provider is seconds per call. Within one
# render the bounded set runs concurrently (it's I/O), so wall-time is ~one call, not their sum.
_MAX_EXPLAIN_WORKERS = 8

# A "why this" reason is a single sentence — cap the response hard so a chatty model can't run up
# the token bill (and the spoiler post-check already rejects anything that overruns this anyway).
_REASON_MAX_TOKENS = 80

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
            candidate = self.llm.complete(  # type: ignore[union-attr]
                _llm_prompt(rec, taste), max_tokens=_REASON_MAX_TOKENS
            ).strip()
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


def stream_lazy_reason(
    rec: Recommendation,
    taste: Mapping[str, Any],
    llm: LLMProvider | None,
    cache: ReasonCache | None,
) -> Iterator[str]:
    """Streaming variant of :func:`lazy_reason`: yields the reason chunk-by-chunk so the detail
    sheet fills in as the model types, instead of waiting for the whole blob. A cached reason comes
    back as one chunk (instant re-open); offline yields the template. Like the chat reply, this
    relies on the prompt for spoiler-safety (it streams before the full text exists) and only caches
    a completed, marker-free result."""
    key = ("reason", str(rec.title_id), _taste_fingerprint(taste))
    if cache is not None and (hit := cache.get(key)) is not None:
        yield str(hit)
        return
    if llm is None:
        yield _template(rec, taste)
        return
    chunks: list[str] = []
    try:
        for chunk in stream_text(llm, _llm_prompt(rec, taste), max_tokens=_REASON_MAX_TOKENS):
            chunks.append(chunk)
            yield chunk
    except Exception:  # noqa: BLE001 - a flaky model must not sink the request
        logger.warning("recommend.reason_failed", extra={"title": rec.title})
        if not chunks:
            yield _template(rec, taste)
        return
    full = "".join(chunks).strip()
    if full and _SPOILER_MARKERS.search(full) is None and cache is not None:
        cache.set(key, full)


def _reason_db_key(key: Hashable) -> tuple[uuid.UUID, str] | None:
    """Parse a lazy-reason cache key ``("reason", title_id, fingerprint)`` into its DB primary key.

    Returns ``None`` for any other key shape (e.g. the eager Explainer's keys), so the persistent
    cache transparently no-ops on keys it doesn't own instead of guessing.
    """
    if not (isinstance(key, tuple) and len(key) == 3 and key[0] == "reason"):
        return None
    try:
        return uuid.UUID(str(key[1])), str(key[2])
    except (ValueError, TypeError):
        return None


class PersistentReasonCache:
    """A two-tier cache for lazy "why this" reasons: the in-process :class:`TTLCache` (L1) over a
    Postgres ``title_explanation`` row (L2). Same ``get``/``set`` shape as ``TTLCache``, so it
    drops into :func:`stream_lazy_reason` unchanged.

    Why: the L1 alone is lost on every restart/redeploy, so each accepted reason was re-generated
    (a workhorse call) after each recycle. Persisting it makes generation a once-per-taste-version
    cost. Only the lazy reason path uses this — the eager Explainer stays L1-only by design.
    """

    def __init__(self, session: Session, l1: TTLCache) -> None:
        self._session = session
        self._l1 = l1

    def get(self, key: Hashable) -> Any | None:
        hit = self._l1.get(key)
        if hit is not None:
            return hit
        db_key = _reason_db_key(key)
        if db_key is None:
            return None
        row = self._session.get(TitleExplanation, db_key)
        if row is None:
            return None
        self._l1.set(key, row.explanation)  # warm L1 so the next read skips the DB
        return row.explanation

    def set(self, key: Hashable, value: str) -> None:
        self._l1.set(key, value)
        db_key = _reason_db_key(key)
        if db_key is None:
            return
        title_id, fingerprint = db_key
        existing = self._session.get(TitleExplanation, db_key)
        if existing is not None:
            existing.explanation = value
        else:
            self._session.add(
                TitleExplanation(
                    title_id=title_id, taste_fingerprint=fingerprint, explanation=value
                )
            )
        try:
            self._session.commit()
        except IntegrityError:
            # A concurrent open inserted the same (title, taste) first — its value is just as good.
            self._session.rollback()
