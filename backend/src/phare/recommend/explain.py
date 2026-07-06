"""Explanations: short, spoiler-safe, confidence-aware. LLM when available, else a template.

Hard rules (docs/design.md): describe *appeal* (tone, themes, fit), never plot events of an
unwatched title; express confidence; never cite another user. The template fallback keeps the
whole pipeline working offline; it only ever uses structured metadata (genres, year), never the
free-text overview, so it cannot leak plot.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Hashable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from phare.core.fallback import record_fallback
from phare.core.i18n import DEFAULT_LANGUAGE, Language, llm_output_directive, translate
from phare.db.models import TitleExplanation
from phare.providers.http import TTLCache
from phare.providers.types import LLMProvider, stream_text
from phare.recommend.genres import translate_genre, translate_genres
from phare.recommend.schema import Anchor, Recommendation


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

# Bump when the explanation *prompt* — or the acceptance contract on its output — changes. It's
# folded into the cache fingerprint so a bump invalidates every previously-cached blurb (in-process
# and the durable Postgres rows) without a manual purge — otherwise a deploy keeps serving the old
# phrasing until taste happens to change, since the key is otherwise only the taste summary.
# "2" = the personalised-prompt rewrite; "3" = grounded in synopsis/keywords + honest weak-fit
# framing (review H4); "4" = the wrong-language guard (R5) — entries cached before it exist that
# the guard would now reject (observed: Franglais on a French hero), so flush them; "5" = the
# no-English-opener directive + opener rejection (R6) — v4 entries could still open with "Since";
# "6" = the French vouvoiement directive (R7) — v5 entries could be cached tutoiement.
_PROMPT_VERSION = "6"


def _taste_fingerprint(
    taste: Mapping[str, Any],
    anchor: Anchor | None = None,
    language: Language = DEFAULT_LANGUAGE,
) -> str:
    # Fold the request language into the key: an explanation is generated in the reader's language
    # (LLM output + the offline template), so a cache keyed only on taste would pin whichever
    # language asked first and serve it to the other — a French reason returned for an English
    # request, forever. The persistent L2 (title_explanation rows) inherits this key, so the same
    # split holds across restarts.
    raw = f"{_PROMPT_VERSION}|{language}|{taste.get('summary') or ''}"
    # The anchor segment is appended only when present, so an un-anchored reason keeps the exact
    # same fingerprint it had before anchors existed — only anchored reasons get their own cache
    # bucket (the same title explained "because you watched Dune" vs "...Her" must differ).
    if anchor is not None:
        raw = f"{raw}|anchor={anchor.title_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# Inputs to the LLM are already spoiler-safe (we never pass the overview), but the *output* is the
# model's; a weak/jailbroken model could still narrate plot. This is a cheap last line of defence,
# not a guarantee: a one-line appeal blurb has no business being long or naming a plot reveal.
_MAX_EXPLANATION_LEN = 320
_SPOILER_MARKERS = re.compile(
    r"\b(turns out|plot twist|the twist|is revealed|revealed to be|the killer|the murderer|"
    r"dies|is killed|killed off|betrays|the ending|ending reveals|spoiler|"
    # French — the product is bilingual, so an EN-only net was blind to half the replies (B7). A
    # conservative list; the prompt instruction is the real defence, this is just a backstop.
    r"le tueur|le meurtrier|il meurt|elle meurt|est tué|est tuée|se révèle|la révélation|"
    r"le twist|trahit|le dénouement|à la fin il|à la fin elle)\b",
    re.IGNORECASE,
)


def has_spoiler_markers(text: str) -> bool:
    """True if the text names a plot reveal. Marker-only (no length rule), so it suits a
    multi-sentence chat reply as well as a one-line blurb (review B7)."""
    return _SPOILER_MARKERS.search(text) is not None


# Wrong-language guard (Lot R3). The cache key already splits on language (round 2), but nothing
# checked the *content*: mistral-small answered a French-directive prompt in Franglais ("Since tu
# aimes les thrillers scientifiques…") and that reply got cached permanently, then served to French
# readers by both the eager row path and the lazy stream. Like the spoiler net, this is a cheap,
# deterministic, no-LLM last line of defence on the model's output before we cache it.
#
# Heuristic: score the text by counting strong, unambiguous function words per language and require
# the *requested* language to strictly win. Function words are the giveaway of the prose language,
# and — unlike content words — a proper noun can't masquerade as one, so an English movie *title*
# dropped inside a French sentence ("Comme vous aimez les thrillers, Blade Runner…") contributes
# zero English signal and the French sentence still wins. A tie or a lead for the other language
# rejects — which is exactly what catches the observed English-led Franglais on a French request.
_LANGUAGE_SIGNALS: dict[Language, re.Pattern[str]] = {
    "fr": re.compile(
        r"\b(le|la|les|des|une|un|est|dans|avec|pour|vous|tu|vos|votre|qui|que|ce|cette|"
        r"aime|aimez|goût|goûts)\b",
        re.IGNORECASE,
    ),
    "en": re.compile(
        r"\b(the|a|an|is|are|in|with|for|you|your|this|that|which|who|and|of|to|like|likes|"
        r"taste|since)\b",
        re.IGNORECASE,
    ),
}

# Below this length there's too little prose to judge — a two-word blurb or a bare title has no
# reliable function-word signal in either language, so we don't second-guess it (mirrors the
# spoiler net's "don't over-reject" stance). Kept in sync with the ~40-char task threshold.
_LANGUAGE_MIN_LEN = 40

# A non-English reply must not OPEN with an English connective: the workhorse model's observed
# failure mode is exactly one code-switched leading word ("Since tu aimes…") — invisible to the
# majority count (one word), yet the most prominent word of the most prominent sentence. French
# never opens a sentence with these, so the check is essentially free of false positives.
_ENGLISH_OPENERS = re.compile(
    r"^\s*[*_\"«']*\s*(since|because|as|while|if|although|though|whether|given|when)\b",
    re.IGNORECASE,
)


def is_plausible_language(text: str, language: Language) -> bool:
    """True if ``text`` reads as ``language`` (or is too short to judge). Cheap and deterministic.

    Counts strong function-word signals for the requested language and every *other* supported
    language, and requires the requested one to strictly win. Short texts (< ~40 chars) always pass:
    too little signal to call it. This is a plausibility net, not a language detector — it exists to
    reject the clear cross-language leaks (a French reply to an English request, or English-led
    Franglais to a French one) before they get cached, not to grade grammar."""
    if len(text) < _LANGUAGE_MIN_LEN:
        return True
    requested = _LANGUAGE_SIGNALS.get(language)
    if requested is None:  # a language with no signal table configured — don't gate it
        return True
    # One code-switched opener beats the majority count (it's a single word) but is the most
    # visible defect a reply can have — reject it outright for non-English requests.
    if language != DEFAULT_LANGUAGE and _ENGLISH_OPENERS.match(text):
        return False
    mine = len(requested.findall(text))
    others = max(
        (
            len(pattern.findall(text))
            for lang, pattern in _LANGUAGE_SIGNALS.items()
            if lang != language
        ),
        default=0,
    )
    return mine > others


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


_SYSTEM = """You tell one specific viewer why THEY in particular might enjoy a title — one sentence.

Rules:
- Address the viewer as you and anchor the sentence to THEIR taste: name the genre, theme, or
  mood of theirs it connects to. A reader with different taste must get a different sentence — if
  yours would fit any viewer, it's wrong. Prefer opening with the connection — start from what
  they like, then the title (e.g. Since you lean toward X, this ...).
- Ground your sentence in the synopsis/keywords when given, but describe appeal only: tone, themes,
  mood, the taste fit. NEVER mention plot events or retell the story.
- Never mention other people or users.
- Be honest about the fit. If the fit is described as weak/a stretch below, say so plainly — call it
  a gamble outside their usual taste; do NOT invent qualities to force a perfect-match pitch.
- Output ONLY the sentence as plain text: no quotation marks around it, no markdown, no preamble.
"""

# Below this pool-relative similarity, tell the model to frame the pick as a stretch rather than
# oversell it — the "honesty over engagement" line the un-grounded prompt used to cross (review H4).
_WEAK_FIT = 0.4


def _template(
    rec: Recommendation, taste: Mapping[str, Any], language: Language = DEFAULT_LANGUAGE
) -> str:
    """Deterministic, metadata-only explanation. Spoiler-proof by construction.

    Localised via the catalog; genre names are display-translated (review F3) so a French sentence
    reads "Science-Fiction", not the stored English "Science Fiction". Stored labels stay English
    (they key affinity matching); translation happens only here, at the point of display."""
    spanning = translate(language, "explain.genreSpanning")
    kind = translate(language, f"explain.kind.{rec.kind}")
    era = translate(language, "explain.era", year=rec.year) if rec.year else ""
    if rec.is_swing:
        genres = ", ".join(translate_genres(rec.genres[:2], language)) if rec.genres else spanning
        return translate(language, "explain.swing", genres=genres, kind=kind, era=era)
    matched = _matched_affinity(rec, taste)
    if matched is not None:
        # Name the matched genre once — in the "because" hook — and list only the *other* genres in
        # the descriptor, so we don't read "Sci-Fi, Mystery movie that leans into your taste for
        # Sci-Fi". When it's the only genre, keep it as the descriptor and use the generic profile
        # tail instead of repeating it. Comparison stays on the stored English names; only the
        # displayed strings are translated.
        others = [g for g in rec.genres if g.lower() != matched.lower()][:2]
        if others:
            genres = ", ".join(translate_genres(others, language))
            because = translate(
                language, "explain.becauseAffinity", matched=translate_genre(matched, language)
            )
        else:
            genres = translate_genre(matched, language)
            because = _because_profile(rec, language)
    else:
        genres = ", ".join(translate_genres(rec.genres[:2], language)) if rec.genres else spanning
        because = _because_profile(rec, language)
    return translate(language, "explain.base", genres=genres, kind=kind, era=era, because=because)


# Below "strong fit" (matches fit.ts), a template must not claim "fits your profile" on a shaky
# pick — it hedges instead (review B5). Variants are picked by a deterministic title hash so a strip
# doesn't repeat one line; keep the counts in sync with the explain.because* keys in i18n.
_HEDGE_BELOW = 0.66
_PROFILE_VARIANTS = 3
_HEDGE_VARIANTS = 2


def _because_profile(rec: Recommendation, language: Language) -> str:
    """A varied, confidence-aware 'because you'll like it' tail (review B5)."""
    picked = int(hashlib.sha256(rec.title.encode()).hexdigest(), 16)
    if rec.confidence is not None and rec.confidence < _HEDGE_BELOW:
        return translate(language, f"explain.becauseHedged.{picked % _HEDGE_VARIANTS}")
    return translate(language, f"explain.becauseProfile.{picked % _PROFILE_VARIANTS}")


def _matched_affinity(rec: Recommendation, taste: Mapping[str, Any]) -> str | None:
    """The viewer's strongest liked genre that this title actually shares — the concrete reason it
    scored. ``None`` when nothing overlaps (a swing / off-axis pick)."""
    affinities = taste.get("affinities") or {}
    liked = {key.lower(): float(value) for key, value in affinities.items() if float(value) > 0}
    matches = [g for g in rec.genres if g.lower() in liked]
    if not matches:
        return None
    return max(matches, key=lambda g: liked[g.lower()])


def _taste_hooks(rec: Recommendation, taste: Mapping[str, Any]) -> str:
    """Concrete handles for the model to anchor on — the shared genre affinity and a few stated
    likes — so a weak model has something specific to latch onto instead of describing the film."""
    parts: list[str] = []
    if matched := _matched_affinity(rec, taste):
        parts.append(f"shares their liked genre {matched}")
    likes = [str(x) for x in (taste.get("likes") or []) if str(x).strip()][:3]
    if likes:
        parts.append("they like: " + ", ".join(likes))
    return "; ".join(parts)


def _llm_prompt(
    rec: Recommendation,
    taste: Mapping[str, Any],
    language: Language = DEFAULT_LANGUAGE,
    anchor: Anchor | None = None,
) -> str:
    summary = taste.get("summary") or "(no taste summary yet)"
    genres = ", ".join(rec.genres) or "unknown"
    hooks = _taste_hooks(rec, taste)
    hook_line = f"\nAnchor the sentence on this connection: {hooks}." if hooks else ""
    # Ground the model in the actual content so it can't invent aligned qualities (review H4). The
    # synopsis is for understanding only — the prompt forbids retelling plot, and the output is
    # spoiler-screened. Truncate it so a long synopsis can't crowd the instruction out.
    synopsis = (rec.overview or "").strip()
    synopsis_line = (
        f"\nSynopsis (grounding only — describe appeal, NEVER retell the plot): {synopsis[:360]}"
        if synopsis
        else ""
    )
    keywords = ", ".join(k for k in rec.keywords[:8] if k)
    keyword_line = f"\nKeywords: {keywords}" if keywords else ""
    # Real fit signal, so a weak pick is framed honestly instead of oversold. Swing → always a
    # stretch; otherwise gauge by the pool-relative similarity (M1.4) when the breakdown carries it.
    sim_rel = rec.components.get("similarity_rel")
    if rec.is_swing:
        fit = "a deliberate discovery pick — a stretch outside their usual taste"
    elif sim_rel is not None and sim_rel < _WEAK_FIT:
        fit = (
            "a WEAK fit — be honest and frame it as a stretch/gamble, do NOT oversell it as a match"
        )
    else:
        fit = "a strong match"
    anchor_line = ""
    if anchor is not None:
        anchor_genres = ", ".join(anchor.genres) if anchor.genres else "a title they loved"
        anchor_line = (
            f"\nThis is being recommended because they watched and loved {anchor.title} "
            f"({anchor_genres}). Open the sentence from that link — name what {anchor.title} and "
            f"this share (tone, themes, mood). {anchor.title} is a title, not a person."
        )
    directive = llm_output_directive(language)
    tail = f" {directive}" if directive else ""
    title_line = f"Title: {rec.title} ({rec.year or 'n/a'}) — genres: {genres}"
    return (
        f"{_SYSTEM}\nViewer taste: {summary}{hook_line}{anchor_line}\n"
        f"{title_line}{keyword_line}{synopsis_line}\n"
        f"This is {fit}. Write the one sentence, addressed to the viewer as you.{tail}"
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
    language: Language = DEFAULT_LANGUAGE

    def explain(
        self, recommendations: Sequence[Recommendation], taste: Mapping[str, Any]
    ) -> list[Recommendation]:
        """Attach an explanation to each recommendation, returning a new list.

        Cached/templated items resolve inline; the uncached LLM calls (bounded by ``budget``) are
        decided sequentially — so spend is deterministic and front-loaded onto the top-ranked
        items — then fired concurrently.
        """
        fingerprint = _taste_fingerprint(taste, language=self.language)
        texts: list[str | None] = [None] * len(recommendations)
        to_generate: list[tuple[int, Recommendation, tuple[str, bool, str]]] = []
        for i, rec in enumerate(recommendations):
            if self.llm is None:
                texts[i] = _template(rec, taste, self.language)
                continue
            key = (str(rec.title_id), bool(rec.is_swing), fingerprint)
            if self.cache is not None and (cached := self.cache.get(key)) is not None:
                texts[i] = cached
            elif self.budget > 0:
                self.budget -= 1  # bound *calls*, whether or not the output is accepted
                to_generate.append((i, rec, key))
            else:
                texts[i] = _template(rec, taste, self.language)

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

        *Stable* outcomes are cached (an accepted blurb, or a template chosen because the model
        reliably narrates plot for this title — re-attempting either would just re-burn budget). A
        *transient* LLM error is deliberately **not** cached, so the next render re-attempts once
        the provider recovers, instead of serving the template for the whole taste version (G2).
        """
        text, outcome = self._call_or_template(rec, taste)
        if self.cache is not None and outcome != "llm_error":
            self.cache.set(key, text)
        return text

    def _call_or_template(self, rec: Recommendation, taste: Mapping[str, Any]) -> tuple[str, str]:
        """Return ``(text, outcome)`` where outcome is ``ok`` / ``spoiler_rejected`` /
        ``wrong_language`` / ``llm_error`` — the caller uses it to decide whether to cache."""
        try:
            candidate = self.llm.complete(  # type: ignore[union-attr]
                _llm_prompt(rec, taste, self.language), max_tokens=_REASON_MAX_TOKENS
            ).strip()
        except Exception:  # noqa: BLE001 - never let a flaky LLM sink the whole row
            record_fallback("explain", "llm_error", title=rec.title)
            return _template(rec, taste, self.language), "llm_error"
        safe = coerce_safe(candidate) if candidate else None
        if safe:
            if not is_plausible_language(safe, self.language):
                # The model answered in (or drifted into) the wrong language for this request —
                # a stable outcome for this (title, language), so cache the correct-language
                # template rather than pin the Franglais blurb the cache key can't tell apart.
                record_fallback(
                    "explain", "wrong_language", title=rec.title, language=self.language
                )
                return _template(rec, taste, self.language), "wrong_language"
            return safe, "ok"
        if candidate:
            # A genuine plot-reveal marker — a stable outcome for this title; cache the template.
            record_fallback("explain", "spoiler_rejected", title=rec.title)
            return _template(rec, taste, self.language), "spoiler_rejected"
        # Empty completion (a model hiccup, not a reliable spoiler) — transient, so don't cache it.
        record_fallback("explain", "llm_error", title=rec.title)
        return _template(rec, taste, self.language), "llm_error"


def explain(
    recommendations: Sequence[Recommendation],
    taste: Mapping[str, Any],
    llm: LLMProvider | None,
    language: Language = DEFAULT_LANGUAGE,
) -> list[Recommendation]:
    """Explain each recommendation. Back-compat shim: unbounded, uncached (per-call behavior)."""
    return Explainer(llm=llm, language=language).explain(recommendations, taste)


def stream_lazy_reason(
    rec: Recommendation,
    taste: Mapping[str, Any],
    llm: LLMProvider | None,
    cache: ReasonCache | None,
    language: Language = DEFAULT_LANGUAGE,
    anchor: Anchor | None = None,
) -> Iterator[str]:
    """Streaming variant of :func:`lazy_reason`: yields the reason chunk-by-chunk so the detail
    sheet fills in as the model types, instead of waiting for the whole blob. A cached reason comes
    back as one chunk (instant re-open); offline yields the template. Like the chat reply, this
    relies on the prompt for spoiler-safety (it streams before the full text exists) and only caches
    a completed, marker-free result.

    ``anchor`` (the "because you watched X" seed, when the card was opened from such a row) is
    folded into both the prompt and the cache key, so the same title gets a distinct, sharper
    reason per anchor — and the template fallback stays anchor-agnostic (it can't leak plot)."""
    key = ("reason", str(rec.title_id), _taste_fingerprint(taste, anchor, language))
    if cache is not None and (hit := cache.get(key)) is not None:
        yield str(hit)
        return
    if llm is None:
        yield _template(rec, taste, language)
        return
    chunks: list[str] = []
    try:
        for chunk in stream_text(
            llm, _llm_prompt(rec, taste, language, anchor), max_tokens=_REASON_MAX_TOKENS
        ):
            chunks.append(chunk)
            yield chunk
    except Exception:  # noqa: BLE001 - a flaky model must not sink the request
        record_fallback("explain_stream", "llm_error", title=rec.title)
        if not chunks:
            yield _template(rec, taste, language)
        return
    full = "".join(chunks).strip()
    if not full or cache is None:
        return
    if _SPOILER_MARKERS.search(full) is not None:
        return  # a plot-reveal leaked through the stream — don't cache it (prompt is the defence)
    if not is_plausible_language(full, language):
        # Wrong-language completion (the streamed Franglais bug): the reader already saw it live,
        # but don't pin it — leave the entry empty so the next open regenerates, same as spoiler.
        record_fallback("explain_stream", "wrong_language", title=rec.title, language=language)
        return
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
