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
    "explain.genreSpanning": {"en": "Genre-spanning", "fr": "multi-genres"},
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
        # Opens on the (capitalised) genre rather than an article, so it reads right whatever the
        # genre is — "A Animation movie" was the giveaway that a naive "A {…}" was wrong.
        "en": "{genres} {kind}{era}{because}.",
        "fr": "{kind} {genres}{era}{because}.",
    },
    "explain.swing": {
        "en": (
            "{genres} {kind}{era} — a discovery pick outside your usual lane, "
            "offered as a deliberate stretch rather than a sure thing."
        ),
        "fr": (
            "{kind} {genres}{era} — une découverte hors de vos sentiers battus, "
            "proposée comme un pari assumé plutôt qu'une valeur sûre."
        ),
    },
    # Deterministic taste summary, used when the LLM returns nothing parseable (e.g. a reasoning
    # model that spent its budget thinking). Genre names interpolate in their stored language.
    "taste.fallbackSummary": {
        "en": "From what you've watched, you lean toward {leaning}.",
        "fr": "D'après vos visionnages, vous penchez pour {leaning}.",
    },
    "taste.fallbackSummaryEmpty": {
        "en": "Not enough watch history yet to read your taste.",
        "fr": "Pas encore assez d'historique pour cerner vos goûts.",
    },
    # Deterministic chat replies (the offline path, and the off-topic steer-back used even with an
    # LLM). Genre descriptors interpolated in stay in their stored language.
    "chat.decline": {
        "en": (
            "I'm just your movie & TV sidekick — I can't help with that one, but tell me what "
            "you're in the mood to watch and I'll find something."
        ),
        "fr": (
            "Je ne suis que votre acolyte ciné & séries — je ne peux pas vous aider là-dessus, "
            "mais dites-moi ce que vous avez envie de regarder et je trouverai quelque chose."
        ),
    },
    "chat.gotIt": {"en": "Got it — {actions}.", "fr": "C'est noté — {actions}."},
    "chat.herePicks": {
        "en": "Here are a few picks you might enjoy.",
        "fr": "Voici quelques suggestions qui pourraient vous plaire.",
    },
    "chat.noMatch": {
        "en": "I couldn't find a good match — try loosening the constraints a little.",
        "fr": "Je n'ai pas trouvé de bonne correspondance — essayez d'élargir un peu les critères.",
    },
    "chat.done": {"en": "Done.", "fr": "C'est fait."},
    # The always-present escape hatch on a clarifying question — the user never has to answer it.
    "chat.surpriseMe": {"en": "Surprise me", "fr": "Surprends-moi"},
    "chat.offlineNoMatch": {
        "en": "I couldn't find a good match for that — try loosening the constraints a little.",
        "fr": "Je n'ai rien trouvé de pertinent — essayez d'assouplir un peu les critères.",
    },
    "chat.offlinePicks": {
        "en": "Here are a few {descriptor}picks{runtime} you might enjoy.",
        "fr": "Voici quelques suggestions {descriptor}{runtime} qui pourraient vous plaire.",
    },
    "chat.runtimeUnder": {"en": " under {minutes} minutes", "fr": " de moins de {minutes} minutes"},
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


def llm_output_directive(language: Language) -> str:
    """A one-line instruction telling the model which language to write its user-facing text in.

    Empty for the English source language (the prompts are already English). Appended to a prompt
    rather than translating the whole system prompt, so the carefully-worded scope/guardrail text
    stays exactly as written and only the output language changes."""
    if language == DEFAULT_LANGUAGE:
        return ""
    return f"Write your response in {LANGUAGE_NAMES[language]}."
