"""Parse a chat message into a structured :class:`ChatIntent`.

LLM extraction when configured; otherwise a deterministic keyword parser so the chat flow works
offline (and is unit-testable without a key). The keyword parser is intentionally small — it
only needs to catch the common "tired, 90 min, funny" shape, not understand language.
"""

from __future__ import annotations

import json
import logging
import re

from phare.agent.schema import ChatIntent
from phare.providers.types import LLMProvider

logger = logging.getLogger(__name__)

# Mood / genre words -> canonical TMDB genre names used across the catalog.
_MOOD_TO_GENRE: dict[str, str] = {
    "funny": "Comedy",
    "comedy": "Comedy",
    "comedic": "Comedy",
    "laugh": "Comedy",
    "scary": "Horror",
    "horror": "Horror",
    "creepy": "Horror",
    "sad": "Drama",
    "emotional": "Drama",
    "drama": "Drama",
    "action": "Action",
    "thrilling": "Action",
    "thriller": "Thriller",
    "tense": "Thriller",
    "sci-fi": "Science Fiction",
    "scifi": "Science Fiction",
    "space": "Science Fiction",
    "fantasy": "Fantasy",
    "magical": "Fantasy",
    "romantic": "Romance",
    "romance": "Romance",
    "western": "Western",
    "animated": "Animation",
    "animation": "Animation",
    "mystery": "Mystery",
}

_RUNTIME_RE = re.compile(r"(\d{2,3})\s*(?:m|min|mins|minute|minutes)\b", re.IGNORECASE)
_HOUR_RE = re.compile(r"(\d)\s*(?:h|hr|hrs|hour|hours)\b", re.IGNORECASE)
# A "short" film is conventionally feature-length-ish; treat the word as a 100-minute ceiling.
_SHORT_CEILING = 100


def _parse_runtime(text: str) -> int | None:
    if match := _RUNTIME_RE.search(text):
        return int(match.group(1))
    if match := _HOUR_RE.search(text):
        return int(match.group(1)) * 60
    if re.search(r"\bshort\b", text, re.IGNORECASE):
        return _SHORT_CEILING
    return None


def keyword_intent(message: str) -> ChatIntent:
    """Deterministic fallback parser. Never raises; returns an empty intent if nothing matches."""
    lowered = message.lower()
    include: list[str] = []
    exclude: list[str] = []
    for word, genre in _MOOD_TO_GENRE.items():
        if not re.search(rf"\b{re.escape(word)}\b", lowered):
            continue
        # "no horror" / "not scary" / "avoid westerns" -> exclude
        negated = re.search(
            rf"\b(no|not|without|avoid|except)\b[\w\s]{{0,12}}{re.escape(word)}", lowered
        )
        target = exclude if negated else include
        if genre not in target:
            target.append(genre)
    include = [g for g in include if g not in exclude]
    return ChatIntent(
        max_runtime=_parse_runtime(lowered),
        include_genres=include,
        exclude_genres=exclude,
        mood=message.strip() or None,
    )


_LLM_PROMPT = """Extract a structured filter from a viewer's request for something to watch.

Output ONLY a JSON object with keys:
- max_runtime: integer minutes, or null
- include_genres: array of TMDB genre names they want (e.g. "Comedy", "Science Fiction")
- exclude_genres: array of genres to avoid
- mood: short string describing their mood, or null

Request: """


def parse_intent(message: str, llm: LLMProvider | None) -> ChatIntent:
    """LLM extraction when available, else the keyword fallback. Always returns a valid intent."""
    if llm is not None:
        try:
            raw = llm.complete(_LLM_PROMPT + message, max_tokens=150)
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("```", 2)[1].removeprefix("json").strip()
            return ChatIntent.model_validate(json.loads(text))
        except Exception:  # noqa: BLE001 - fall back rather than fail the chat turn
            logger.warning("agent.intent_llm_failed; using keyword fallback")
    return keyword_intent(message)
