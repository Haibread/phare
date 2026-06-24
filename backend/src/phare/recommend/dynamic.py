"""Dynamic, LLM-picked rows — the cheap differentiator (docs/design.md).

The LLM only *steers*: it names the day's themes ("late-October horror you'd tolerate") from the
taste profile + the calendar. The engine still *ranks* each theme through the same retrieval +
re-ranker. Without an LLM, a deterministic calendar+taste fallback picks the themes, so the
feature works offline.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from phare.core.i18n import DEFAULT_LANGUAGE, LANGUAGE_NAMES, Language, translate
from phare.db.models import ROW_KEY_MAX_LEN
from phare.llm_json import extract_json
from phare.providers.http import TTLCache
from phare.providers.types import LLMProvider
from phare.recommend.explain import _taste_fingerprint
from phare.recommend.log import log_rows
from phare.recommend.schema import Candidate, Row

if TYPE_CHECKING:
    from phare.recommend.service import RecommendationService

logger = logging.getLogger(__name__)

# "Today's picks" should be stable for the day, not re-proposed (an LLM call) on every home render.
# Caching themes per (taste, day) removes the recurring theme call AND stabilises the per-theme
# title sets so the explanation cache can warm. Re-keys when taste changes or the day rolls over.
_THEME_CACHE: TTLCache = TTLCache(ttl=86_400, maxsize=512)


class Theme(BaseModel):
    """One dynamic row: a label plus the genre lens the engine fills it through."""

    key: str
    title: str
    include_genres: list[str] = []
    swing_slots: int = 1


# Seasonal lens by month (Northern-hemisphere calendar; a reasonable default for v1). Each entry is
# (stable row key, catalog message key, genres) — the key stays language-independent so logs/dedup
# don't fork per language, while the displayed title is localised.
_SEASONAL: dict[int, tuple[str, str, list[str]]] = {
    2: ("february-romance", "theme.februaryRomance", ["Romance"]),
    6: ("summer-blockbusters", "theme.summerBlockbusters", ["Action", "Adventure"]),
    7: ("summer-blockbusters", "theme.summerBlockbusters", ["Action", "Adventure"]),
    8: ("summer-blockbusters", "theme.summerBlockbusters", ["Action", "Adventure"]),
    10: ("spooky-season", "theme.spookySeason", ["Horror"]),
    12: ("holiday-viewing", "theme.holidayViewing", ["Family", "Fantasy"]),
}


def _slug(text: str) -> str:
    """Stable ``dyn:`` row key from a (possibly long, LLM-supplied) theme title.

    The key is persisted into ``RecommendationLog.row_key`` so it must fit ``ROW_KEY_MAX_LEN``.
    When the slug is too long it's truncated with a short hash suffix so distinct titles keep
    distinct keys.
    """
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "row"
    key = f"dyn:{base}"
    if len(key) <= ROW_KEY_MAX_LEN:
        return key
    suffix = hashlib.sha1(base.encode()).hexdigest()[:8]
    head = base[: ROW_KEY_MAX_LEN - len("dyn:") - 1 - len(suffix)].strip("-")
    return f"dyn:{head}-{suffix}"


def _top_affinity_genre(taste: Mapping[str, Any]) -> str | None:
    affinities = taste.get("affinities") or {}
    positive = {k: float(v) for k, v in affinities.items() if float(v) > 0}
    return max(positive, key=positive.get) if positive else None  # type: ignore[arg-type]


def _fallback_themes(
    taste: Mapping[str, Any], now: datetime, language: Language = DEFAULT_LANGUAGE
) -> list[Theme]:
    themes: list[Theme] = []
    if seasonal := _SEASONAL.get(now.month):
        key, message_key, genres = seasonal
        themes.append(
            Theme(key=_slug(key), title=translate(language, message_key), include_genres=genres)
        )
    if (genre := _top_affinity_genre(taste)) is not None:
        themes.append(
            Theme(
                key=_slug(f"more {genre}"),
                title=translate(language, "theme.moreGenre", genre=genre),
                include_genres=[genre],
            )
        )
    # Always reserve a discovery row — swing-heavy, no genre lens.
    themes.append(
        Theme(
            key="dyn:something-different",
            title=translate(language, "theme.somethingDifferent"),
            swing_slots=3,
        )
    )
    # Dedup by key, keep at most three.
    seen: set[str] = set()
    unique = [t for t in themes if not (t.key in seen or seen.add(t.key))]
    return unique[:3]


_LLM_PROMPT = """Propose up to 3 themed movie/TV recommendation rows for today ({date}).
Consider the season and the viewer's taste. Output ONLY a JSON array of objects with keys:
- title: a short row label (e.g. "Late-October horror you'd tolerate"){title_language}
- genres: array of TMDB genre names to draw from (may be empty for a wildcard discovery row).
  Genre names MUST stay in English (they key the catalog).

Viewer taste: {summary}
Avoid: {avoid}
"""


def propose_themes(
    taste: Mapping[str, Any],
    now: datetime,
    llm: LLMProvider | None,
    language: Language = DEFAULT_LANGUAGE,
) -> tuple[list[Theme], bool]:
    """LLM-picked themes when available, else the deterministic calendar+taste fallback.

    Returns ``(themes, degraded)``. ``degraded`` is True only when an LLM *was* configured but its
    output couldn't be used — a fallback masking a real failure, which the UI flags. Running with no
    LLM at all is the honest offline path, not degradation, so it reports False.
    """
    if llm is None:
        return _fallback_themes(taste, now, language), False
    title_language = (
        f" Write each title in {LANGUAGE_NAMES[language]}." if language != DEFAULT_LANGUAGE else ""
    )
    prompt = _LLM_PROMPT.format(
        date=now.date().isoformat(),
        title_language=title_language,
        summary=taste.get("summary") or "(unknown)",
        avoid=", ".join(taste.get("hard_avoids") or []) or "(nothing specific)",
    )
    try:
        parsed = extract_json(llm.complete(prompt, max_tokens=250))
        themes = [
            Theme(
                key=_slug(str(item["title"])),
                title=str(item["title"]),
                include_genres=[str(g) for g in item.get("genres", [])],
            )
            for item in parsed
            if item.get("title")
        ]
        if themes:
            return themes[:3], False
        return _fallback_themes(taste, now, language), True  # parsed, but nothing usable
    except Exception:  # noqa: BLE001 - never let a flaky LLM kill the row set
        logger.warning("recommend.dynamic_llm_failed; using fallback themes")
        return _fallback_themes(taste, now, language), True


def _genre_filter(genres: Sequence[str]):
    """Keep candidates matching any theme genre; fall back to all if that would empty the pool."""
    if not genres:
        return None
    wanted = {g.lower() for g in genres}

    def apply(candidates: list[Candidate]) -> list[Candidate]:
        matched = [c for c in candidates if wanted & {g.lower() for g in c.genres}]
        return matched or candidates

    return apply


def dynamic_rows(
    service: RecommendationService,
    profile_id: uuid.UUID,
    *,
    llm: LLMProvider | None = None,
    now: datetime | None = None,
) -> tuple[list[Row], bool]:
    """Build today's themed rows. Each theme is filled through the same engine.

    Returns ``(rows, degraded)`` — ``degraded`` True when a configured LLM couldn't name the themes
    and the deterministic fallback stepped in (so the UI can flag it). See :func:`propose_themes`.
    """
    service.ensure_embeddings()
    taste = service.load_taste(profile_id)
    when = now or datetime.now(UTC)
    # Stable for the day per taste + language — see _THEME_CACHE. Stamps the recurring LLM theme
    # call out; the language is in the key so an EN render can't serve FR its cached themes.
    theme_key = (_taste_fingerprint(taste), when.date().isoformat(), service.language)
    themes, degraded = _THEME_CACHE.get_or_set(
        theme_key, lambda: propose_themes(taste, when, llm, service.language)
    )

    # One bounded explainer pooled across the themed rows — same discipline as the static home
    # rows. Without it each theme's recommend() would build its own *unbounded* explainer and fan
    # out a per-item LLM explanation call for every card (slow + costly on a cold cache).
    explainer = service._explainer(
        with_llm=service.chat_llm is not None, budget=service.explanation_budget
    )
    rows: list[Row] = []
    for theme in themes:
        items = service.recommend(
            profile_id,
            taste=taste,
            candidate_filter=_genre_filter(theme.include_genres),
            swing_slots=theme.swing_slots,
            explainer=explainer,
        )
        if items:
            rows.append(Row(key=theme.key, title=theme.title, items=items))
    log_rows(service.session, profile_id, rows)
    logger.info("recommend.dynamic", extra={"profile_id": str(profile_id), "rows": len(rows)})
    return rows, degraded
