"""Backend localization: pick a request language and translate the deterministic strings.

The frontend sends ``Accept-Language: en`` or ``fr``; :func:`parse_accept_language` resolves it to a
supported :data:`Language`. :func:`translate` renders the small catalog of backend-built, non-LLM
strings (row titles, the offline explanation templates). LLM-generated text is localised separately
by instructing the model in the request language — not from this catalog.

Stored catalog metadata (genre names, overviews) keeps whatever language it was imported in; only
freshly-fetched TMDB data honours the request language (see ``providers/tmdb`` ``language``).
"""

from __future__ import annotations

from typing import Literal, get_args

Language = Literal["en", "fr"]

SUPPORTED_LANGUAGES: tuple[Language, ...] = get_args(Language)
DEFAULT_LANGUAGE: Language = "en"

# The model-facing name of each language, used to instruct the LLM which language to write in.
LANGUAGE_NAMES: dict[Language, str] = {"en": "English", "fr": "French"}


def parse_accept_language(header: str | None, default: Language = DEFAULT_LANGUAGE) -> Language:
    """Resolve an ``Accept-Language`` header to a supported language (first match wins).

    Quality values are ignored — the frontend sends a single bare tag ("fr"), and falling back to
    the first supported primary subtag is enough for browsers too ("fr-CA,fr;q=0.9" -> "fr")."""
    if not header:
        return default
    for part in header.split(","):
        primary = part.split(";", 1)[0].strip().lower().split("-", 1)[0]
        for supported in SUPPORTED_LANGUAGES:
            if primary == supported:
                return supported
    return default


# Catalog of deterministic, backend-built strings. Each entry maps a key to a per-language template
# (``str.format`` placeholders). English is the source of truth and the fallback for a missing key.
_MESSAGES: dict[str, dict[Language, str]] = {
    # Row titles.
    "row.youMightLike": {"en": "You might like", "fr": "Pourrait vous plaire"},
    "row.becauseYouWatched": {
        "en": "Because you watched {title}",
        "fr": "Parce que vous avez regardé {title}",
    },
    "row.watchAgain": {"en": "Watch again", "fr": "À revoir"},
    "row.popular": {"en": "Popular", "fr": "Populaire"},
    "row.continueWatching": {"en": "Continue watching", "fr": "Reprendre"},
    # Static row explanations.
    "explain.watchAgain": {
        "en": "You rated this highly — worth another night.",
        "fr": "Vous l'avez bien noté — à revoir un de ces soirs.",
    },
    "explain.popular": {"en": "Popular right now.", "fr": "Populaire en ce moment."},
    "explain.continueWatching": {
        "en": "Pick up where you left off.",
        "fr": "Reprenez où vous en étiez.",
    },
    # Template (offline) explanation fragments. Assembled into one sentence in recommend/explain.py.
    "explain.kind.movie": {"en": "movie", "fr": "Film"},
    "explain.kind.show": {"en": "show", "fr": "Série"},
    "explain.genreSpanning": {"en": "genre-spanning", "fr": "multi-genres"},
    "explain.era": {"en": " from {year}", "fr": " de {year}"},
    "explain.becauseAffinity": {
        "en": " that leans into your taste for {matched}",
        "fr": " qui rejoint votre goût pour {matched}",
    },
    "explain.becauseProfile": {
        "en": " that fits your profile",
        "fr": " qui correspond à votre profil",
    },
    "explain.base": {
        "en": "A {genres} {kind}{era}{because}.",
        "fr": "{kind} {genres}{era}{because}.",
    },
    "explain.swing": {
        "en": (
            "A {genres} {kind}{era} — a discovery pick outside your usual lane, "
            "offered as a deliberate stretch rather than a sure thing."
        ),
        "fr": (
            "{kind} {genres}{era} — une découverte hors de vos sentiers battus, "
            "proposée comme un pari assumé plutôt qu'une valeur sûre."
        ),
    },
    # Dynamic-row fallback theme titles (used offline / when the LLM proposes nothing).
    "theme.februaryRomance": {"en": "February romance", "fr": "Romance de février"},
    "theme.summerBlockbusters": {"en": "Summer blockbusters", "fr": "Blockbusters de l'été"},
    "theme.spookySeason": {"en": "Spooky season", "fr": "Saison des frissons"},
    "theme.holidayViewing": {"en": "Holiday viewing", "fr": "Programme des fêtes"},
    "theme.moreGenre": {"en": "More {genre} for you", "fr": "Plus de {genre} pour vous"},
    "theme.somethingDifferent": {"en": "Something different", "fr": "Quelque chose de différent"},
}


def translate(language: Language, key: str, /, **kwargs: object) -> str:
    """Render a catalog string in ``language`` (English fallback), applying ``str.format``."""
    variants = _MESSAGES[key]
    template = variants.get(language) or variants[DEFAULT_LANGUAGE]
    return template.format(**kwargs) if kwargs else template
