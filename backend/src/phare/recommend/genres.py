"""Canonical genre/term matching — shared by the chat genre filter and taste-affinity scoring.

The planner LLM emits free-form genre words ("sci-fi"), the catalog stores TMDB labels ("Science
Fiction", "Sci-Fi & Fantasy"), and the taste extractor emits free affinity keys. Plain lowercase
string intersection misses every one of these (review A2 for the genre filter, H1 for affinities).
This module is the single normalization + match rule everyone routes through, plus the visible
signal to emit when a genre filter matches nothing and falls back (review A3/G1).

Match rule (same for the genre filter and taste affinities/hard-avoids): a term matches a token when
their alias-resolved canonical forms are equal, **or** one is a substring of the other of length ≥ 4
(the threshold stops "war" matching "wardrobe"; below it, equality is required).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from phare.agent.intent import _MOOD_TO_GENRE
from phare.core.fallback import record_fallback
from phare.core.i18n import DEFAULT_LANGUAGE, Language

_SEP_RE = re.compile(r"[/_-]+")
_WS_RE = re.compile(r"\s+")
# Below this length, a substring match is too loose (e.g. "war" ⊂ "wardrobe"), so require equality.
_MIN_SUBSTRING = 4


def _normalize(term: str) -> str:
    """Lowercase, trim, and flatten separators (``/ - _``) + runs of whitespace to single spaces."""
    return _WS_RE.sub(" ", _SEP_RE.sub(" ", term.strip().lower())).strip()


# Free genre/mood words -> canonical genre, in normalized form. Reuses the English mood map from the
# keyword parser (single source of truth) and adds the French genre labels — the product is
# bilingual, so "horreur" must resolve to the catalog's English "Horror".
_ALIASES: dict[str, str] = {_normalize(k): _normalize(v) for k, v in _MOOD_TO_GENRE.items()}
_ALIASES.update(
    {
        # French genre labels -> canonical (English) TMDB genre names, normalized.
        "horreur": "horror",
        "épouvante": "horror",
        "comédie": "comedy",
        "drame": "drama",
        "aventure": "adventure",
        "documentaire": "documentary",
        "famille": "family",
        "familial": "family",
        "fantastique": "fantasy",
        "fantaisie": "fantasy",
        "histoire": "history",
        "musique": "music",
        "mystère": "mystery",
        "policier": "crime",
        "guerre": "war",
        "science fiction": "science fiction",
        "science-fiction": "science fiction",
        # extra English variants not in the mood map
        "scifi": "science fiction",
    }
)


# The closed vocabulary the taste extractor is asked to draw affinity/hard-avoid keys from, so they
# line up with what candidates are actually tagged with (review H1: free keys matched nothing).
# Genres carry the real steering weight (they match candidate genres); the descriptors are a small
# controlled tone/era set so a key like "Mind-bending Sci-Fi" still resolves to something known.
CANONICAL_GENRES: tuple[str, ...] = (
    "Action",
    "Adventure",
    "Animation",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "History",
    "Horror",
    "Music",
    "Mystery",
    "Romance",
    "Science Fiction",
    "Thriller",
    "War",
    "Western",
    "Sci-Fi & Fantasy",
    "War & Politics",
    "Action & Adventure",
    "Kids",
    "Reality",
)
AFFINITY_KEYWORDS: tuple[str, ...] = (
    "slow-burn",
    "fast-paced",
    "atmospheric",
    "cerebral",
    "mind-bending",
    "character-driven",
    "plot-driven",
    "dark",
    "feel-good",
    "violent",
    "gory",
    "wholesome",
    "romantic",
    "nostalgic",
    "stylish",
    "gritty",
    "epic",
    "minimalist",
    "franchise",
    "blockbuster",
    "indie",
    "arthouse",
    "cult",
    "prestige",
    "campy",
    "psychological",
    "dystopian",
    "coming-of-age",
    "true-story",
    "ensemble",
)
CLOSED_VOCABULARY: tuple[str, ...] = CANONICAL_GENRES + AFFINITY_KEYWORDS

# Static display translations for TMDB genre names — templates and card metadata store the English
# label (it keys affinity matching against the catalog), so the localized string is applied only at
# display time (review F3). A name with no entry falls back to English + a fallback signal, so a new
# TMDB genre never leaves a hole in the sentence.
_GENRE_TRANSLATIONS: dict[Language, dict[str, str]] = {
    "fr": {
        "Action": "Action",
        "Adventure": "Aventure",
        "Animation": "Animation",
        "Comedy": "Comédie",
        "Crime": "Crime",
        "Documentary": "Documentaire",
        "Drama": "Drame",
        "Family": "Familial",
        "Fantasy": "Fantastique",
        "History": "Histoire",
        "Horror": "Horreur",
        "Music": "Musique",
        "Mystery": "Mystère",
        "Romance": "Romance",
        "Science Fiction": "Science-Fiction",
        "Thriller": "Thriller",
        "War": "Guerre",
        "Western": "Western",
        "Sci-Fi & Fantasy": "Science-Fiction & Fantastique",
        "War & Politics": "Guerre & Politique",
        "Action & Adventure": "Action & Aventure",
        "Kids": "Enfants",
        "Reality": "Téléréalité",
    }
}


def translate_genre(name: str, language: Language) -> str:
    """Localize a stored (English) TMDB genre name for display. Unknown names fall back to English
    and emit a fallback signal, so the sentence never shows a blank (review F3, G1)."""
    if language == DEFAULT_LANGUAGE:
        return name
    table = _GENRE_TRANSLATIONS.get(language)
    if table is None:
        return name
    if name in table:
        return table[name]
    record_fallback("genre_translation", "unmapped", genre=name, language=language)
    return name


def translate_genres(names: Iterable[str], language: Language) -> list[str]:
    """``translate_genre`` over a sequence, preserving order."""
    return [translate_genre(name, language) for name in names]


def canonical(term: str) -> str:
    """Normalize a genre/term and resolve a known alias to its canonical form (else the norm)."""
    norm = _normalize(term)
    return _ALIASES.get(norm, norm)


def in_vocabulary(term: str) -> bool:
    """True if a taste key resolves to something in the closed vocabulary under the match rule."""
    return any(term_matches(term, allowed) for allowed in CLOSED_VOCABULARY)


def term_matches(term: str, token: str) -> bool:
    """True if ``term`` matches ``token`` under the shared rule (see module docstring)."""
    ct, cto = _normalize(term), _normalize(token)
    if not ct or not cto:
        return False
    if ct == cto or canonical(term) == canonical(token):
        return True
    shorter, longer = (ct, cto) if len(ct) <= len(cto) else (cto, ct)
    return len(shorter) >= _MIN_SUBSTRING and shorter in longer


def matches_any(terms: Iterable[str], tokens: Iterable[str]) -> bool:
    """True if any term matches any token (order-independent)."""
    token_list = list(tokens)
    return any(term_matches(term, token) for term in terms for token in token_list)


def record_genre_filter_fallback(wanted: Sequence[str], catalog_genres: Iterable[str]) -> None:
    """A genre filter matched nothing and fell back to the unfiltered pool — make it visible.

    A silent fallback (review A3) is what turned the vocabulary bug (A2) into "mediocre for no
    reason": you can't tell a thin catalog from a broken filter. Logs the wanted genres + a sample
    of what the catalog actually had, via the shared fallback convention (G1)."""
    sample = sorted({g for g in catalog_genres if g})[:12]
    record_fallback("genre_filter", "no_match", wanted=list(wanted), catalog_genres_sample=sample)


def record_affinity_key_unmatched(key: str) -> None:
    """A taste affinity/hard-avoid key resolves to nothing in the closed vocabulary — so it can't
    steer scoring (review H1). Make it visible instead of letting the profile silently go inert."""
    record_fallback("taste_affinity", "key_unmatched", key=key)
