"""Deterministic keyword parser for a chat message → :class:`ChatIntent`.

This is the **offline / no-LLM floor only**. When a model is configured the planner
(:mod:`phare.agent.planner`) owns intent — it reads the raw message and emits tool calls (genres,
runtime, rewatch, …) in one call, in any language. So this parser never runs on the LLM path; it
exists so the chat flow still works with zero model access. It's intentionally small — catch the
common "tired, 90 min, funny" shape, not understand language.
"""

from __future__ import annotations

import re

from phare.agent.schema import ChatIntent

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

# A request to revisit something already seen — flips the candidate source to watched history.
_REWATCH_RE = re.compile(
    r"\b(re-?watch\w*|watch\s+again|seen\s+(it\s+)?before|comfort\s+(re-?)?watch|revisit\w*)\b",
    re.IGNORECASE,
)

# Movie vs show, FR + EN. Only a constraint when the message clearly leans one way (not both).
_MOVIE_RE = re.compile(r"\b(movie|movies|film|films)\b", re.IGNORECASE)
_SHOW_RE = re.compile(r"\b(show|shows|series|serie|séries?|tv)\b", re.IGNORECASE)


def _parse_kind(text: str) -> str | None:
    movie, show = bool(_MOVIE_RE.search(text)), bool(_SHOW_RE.search(text))
    if movie and not show:
        return "movie"
    if show and not movie:
        return "show"
    return None  # neither, or both → no constraint


def _parse_runtime(text: str) -> int | None:
    if match := _RUNTIME_RE.search(text):
        return int(match.group(1))
    if match := _HOUR_RE.search(text):
        return int(match.group(1)) * 60
    if re.search(r"\bshort\b", text, re.IGNORECASE):
        return _SHORT_CEILING
    return None


def keyword_intent(message: str) -> ChatIntent:
    """Parse a message into an intent by keyword. Never raises; empty intent if nothing matches."""
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
        # Leave mood null: this keyword parser resolves structured signal (genres/runtime/kind), not
        # a free-form mood. Dumping the raw message here (F2) polluted intent.mood on a declined
        # off-topic turn, where it surfaced in the payload for no functional gain — the offline
        # recommend path never biases on it, and a real mood comes from the LLM planner's recommend
        # tool arg, not from this floor.
        mood=None,
        kind=_parse_kind(lowered),
        rewatch=_REWATCH_RE.search(message) is not None,
    )
