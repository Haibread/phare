"""Taste extraction: turn a profile's events into a structured, editable taste profile.

The LLM reads the fuzzy signal (ratings, abandonment, rewatches, genres) and emits a
validated structured profile. User edits live in ``user_overrides`` and always win.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.core.config import get_settings
from phare.core.fallback import record_fallback
from phare.core.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGE_NAMES,
    Language,
    llm_output_directive,
    translate,
)
from phare.db.models import TasteProfile, Title, WatchEvent
from phare.llm_json import extract_json
from phare.providers.llm import OpenAILLMProvider
from phare.providers.types import LLMProvider
from phare.recommend import genres
from phare.taste.schema import TasteProfileData

logger = logging.getLogger(__name__)

# Taste extraction emits a bounded JSON object (summary + a handful of short arrays); cap the
# response so a chatty model can't run up the token bill on it.
_TASTE_MAX_TOKENS = 700


# ``affinities`` and ``hard_avoids`` are the keys that actually steer scoring, so they must line up
# with what catalog titles are tagged with — a free key ("Mind-bending Sci-Fi") matched nothing and
# left the profile inert (review H1). Constrain them to a closed vocabulary; ``summary`` / ``likes``
# / ``dislikes`` stay free-form natural language so the displayed profile isn't impoverished.
def _prompt_header(language: Language) -> str:
    """The taste-extraction system prompt, with the controlled vocabulary in ``language``.

    The genre vocabulary is presented in the profile's language so the model emits affinity/avoid
    keys the *user* can read (a French profile no longer sports English chips like "cerebral sci-fi"
    under a French summary, review R7). The English genre labels stay in scoring range because
    ``recommend/genres.py`` alias-resolves the French forms back to the catalog's English names
    (``_ALIASES``); free descriptors match less in French, an accepted trade-off (honest,
    user-visible language over hidden matching for the genre-level chips that do the bulk of the
    steering — we do NOT build a translation layer)."""
    allowed_genres = ", ".join(genres.translate_genres(genres.CANONICAL_GENRES, language))
    allowed_keywords = ", ".join(genres.AFFINITY_KEYWORDS)
    return f"""You analyze a viewer's watch history and produce a structured taste profile.

Output ONLY a JSON object with these keys:
- summary: one short paragraph describing the viewer's taste, in plain language, addressed
  DIRECTLY to them in the second person ("You gravitate toward…", "You tend to avoid…") — warm and
  personal, never a third-person report ("The viewer…", "They…")
- likes: array of strings (what they gravitate toward), free-form
- dislikes: array of strings, free-form
- hard_avoids: array of strings to NEVER recommend — each MUST come from the controlled vocabulary
- affinities: object mapping a taste dimension -> weight in [-1, 1]. Every key MUST come from the
  controlled vocabulary below so it lines up with the catalog; prefer genre names (they carry the
  most weight in scoring).
- comfort_axis: short string for their comfort/rewatch leanings (or null)
- discovery_tolerance: number in [0, 1] (appetite for unfamiliar picks)
- confidence: number in [0, 1] based on how much history is available

Controlled vocabulary for `affinities` keys and `hard_avoids`:
- genres: {allowed_genres}
- descriptors: {allowed_keywords}

Guidance: weight recent activity more; treat abandoned/low-rated titles as strong negative
signal and rewatches as comfort signal. Be specific. History (most recent first):
"""


def effective_profile(taste: TasteProfile) -> dict[str, Any]:
    """The structured profile with sticky user edits applied on top."""
    merged = dict(taste.structured)
    merged.update(taste.user_overrides)
    return merged


@dataclass
class _TitleRollup:
    """Accumulates a single title's watch events into one history line for the taste prompt.

    Events arrive most-recent-first; each ``add`` folds one in. The line carries the distinct event
    type(s) seen (in encounter order), a watch count when the title has more than one event, and a
    rating when any event for the title was rated (the most recent one, since events arrive newest
    first).
    """

    title: Title
    count: int = 0
    types: list[str] = field(default_factory=list)
    rating: float | None = None

    def add(self, event: WatchEvent) -> None:
        self.count += 1
        if event.type.value not in self.types:
            self.types.append(event.type.value)
        # Keep the most recent rating: events fold in newest-first, so the first non-null wins.
        if self.rating is None and event.rating is not None:
            self.rating = float(event.rating)

    def line(self) -> str:
        genres = ", ".join(self.title.genres) if self.title.genres else "?"
        rating = f" rating={self.rating:g}" if self.rating is not None else ""
        count = f" x{self.count}" if self.count > 1 else ""
        types = ", ".join(self.types)
        return (
            f"- [{types}{count}] {self.title.title} ({self.title.kind.value}, "
            f"{self.title.year or '?'}) genres: {genres}{rating}"
        )


class TasteService:
    """Generates and persists a profile's taste profile via the LLM."""

    def __init__(
        self,
        session: Session,
        llm: LLMProvider,
        model_version: str,
        language: Language = DEFAULT_LANGUAGE,
    ) -> None:
        self.session = session
        self.llm = llm
        self.model_version = model_version
        self.language = language

    def _history_lines(self, profile_id: uuid.UUID) -> list[str]:
        """One line per *distinct title*, most-recent-first, capped at ``taste_max_events`` titles.

        Trakt history is episode-level, so a binged show would otherwise eat dozens of the (150)
        lines and crowd out every other title the viewer watched — a 5952-event profile surfaced a
        handful of distinct titles to the LLM. Roll the events up per title instead: carry the
        event type(s), a watch count when >1, and a rating when any event for the title is rated.
        """
        rows = self.session.execute(
            select(WatchEvent, Title)
            .join(Title, WatchEvent.title_id == Title.id)
            .where(WatchEvent.profile_id == profile_id, WatchEvent.excluded.is_(False))
            .order_by(WatchEvent.occurred_at.desc().nulls_last())
        ).all()
        # Accumulate per title in first-seen (most-recent-first) order, so the cap keeps the most
        # recent distinct titles rather than the most recent raw events.
        rolled: dict[uuid.UUID, _TitleRollup] = {}
        for event, title in rows:
            rollup = rolled.get(title.id)
            if rollup is None:
                rollup = _TitleRollup(title=title)
                rolled[title.id] = rollup
            rollup.add(event)
        limit = get_settings().taste_max_events
        return [rollup.line() for rollup in list(rolled.values())[:limit]]

    def _memory_block(self, profile_id: uuid.UUID) -> str:
        """Active agent-memory notes the user told us — distilled into the taste profile here."""
        from phare.agent import memory as memory_store

        notes = memory_store.active_notes(self.session, profile_id, now=datetime.now(UTC))
        if not notes:
            return ""
        joined = "; ".join(n.text for n in notes)
        return f"\n\nThe viewer explicitly told us to remember (weight these heavily): {joined}\n"

    def _deterministic(self, profile_id: uuid.UUID) -> TasteProfileData:
        """A taste profile built from genre frequency alone — no LLM.

        The graceful floor when the model returns nothing parseable: the most-watched genres become
        the user's likes and positive affinities (normalised to the peak count), so the profile is
        still inspectable and editable instead of blank. Confidence stays low to mark it as the
        coarse fallback it is.
        """
        rows = self.session.execute(
            select(Title.genres)
            .join(WatchEvent, WatchEvent.title_id == Title.id)
            .where(WatchEvent.profile_id == profile_id, WatchEvent.excluded.is_(False))
        ).all()
        counts: Counter[str] = Counter(g for (genres,) in rows for g in (genres or []))
        peak = max(counts.values(), default=0)
        top = [genre for genre, _ in counts.most_common(5)]
        affinities = {genre: round(count / peak, 2) for genre, count in counts.most_common(8)}
        summary = (
            translate(self.language, "taste.fallbackSummary", leaning=", ".join(top))
            if top
            else translate(self.language, "taste.fallbackSummaryEmpty")
        )
        return TasteProfileData(
            summary=summary,
            likes=top,
            affinities=affinities,
            confidence=round(min(0.5, len(rows) / 40), 2),
        )

    def generate(self, profile_id: uuid.UUID) -> TasteProfile:
        lines = self._history_lines(profile_id)
        history = "\n".join(lines) if lines else "(no history)"
        prompt = _prompt_header(self.language) + history + self._memory_block(profile_id)
        if self.language != DEFAULT_LANGUAGE:
            # Everything the user sees localises — the summary AND the affinity/avoid/like keys, so
            # a French profile isn't studded with English chips (review R7). The genre keys picked
            # from the (now localised) controlled vocabulary above still steer scoring: genres.py
            # alias-resolves the French forms back to catalog English labels. `vous`, not `tu`.
            directive = llm_output_directive(self.language)
            prompt += (
                f"\n\nWrite every string value (summary, likes, dislikes, hard_avoids, and the "
                f"affinity keys) in {LANGUAGE_NAMES[self.language]}, drawing the affinity/avoid "
                f"keys from the controlled vocabulary exactly as spelled above. {directive}"
            )
        logger.info("taste.generate", extra={"profile_id": str(profile_id), "events": len(lines)})

        # temperature=0: taste extraction is structured JSON, not prose — the same history should
        # always distil to the same profile (and the same parse), so don't let sampling wobble it.
        raw = self.llm.complete(prompt, max_tokens=_TASTE_MAX_TOKENS, temperature=0.0)
        degraded = False
        try:
            data = TasteProfileData.model_validate(extract_json(raw))
        except (ValueError, ValidationError):
            degraded = True
            # The model answered but with no parseable JSON — e.g. a reasoning model that spent its
            # token budget thinking, or one that ignored the "JSON only" contract. Degrade to a
            # deterministic profile from genre history (docs/design.md: works on a weak model)
            # rather than 500 the request. A raised exception (LLM unreachable) still propagates so
            # callers like the ingest auto-refresh can swallow it.
            record_fallback(
                "taste_extraction", "unparseable_completion", profile_id=str(profile_id)
            )
            data = self._deterministic(profile_id)

        # Flag any affinity key that resolves to nothing in the closed vocabulary — it can't steer
        # scoring (review H1), so surface it instead of letting the profile quietly go inert.
        for key in data.affinities:
            if not genres.in_vocabulary(key):
                genres.record_affinity_key_unmatched(key)

        taste = self.session.scalar(
            select(TasteProfile).where(TasteProfile.profile_id == profile_id)
        )
        if taste is None:
            taste = TasteProfile(profile_id=profile_id)
            self.session.add(taste)

        taste.summary_text = data.summary
        # The freshly generated summary is in this run's language; seed the per-language cache with
        # it and drop any stale translations from the previous version (review F1).
        taste.summary_by_lang = {self.language: data.summary}
        taste.structured = data.model_dump(mode="json")
        taste.confidence = data.confidence
        taste.model_version = self.model_version
        taste.generated_at = datetime.now(UTC)
        # Mark (or clear) the degraded flag: a fallback profile is re-attempted next refresh; a
        # successful extraction lifts it (review A14). Overrides are left untouched — hand edits
        # survive regeneration either way.
        taste.degraded = degraded
        self.session.flush()
        return taste


# Cap the on-demand summary translation — it's one short paragraph, so a small budget is plenty and
# keeps a chatty model from running up the bill on a read-path call.
_TRANSLATE_MAX_TOKENS = 400


def localized_summary(
    session: Session,
    taste: TasteProfile,
    language: Language,
    llm: LLMProvider | None,
) -> str:
    """The taste summary in ``language``, translating on demand and caching the result so each
    language costs at most one workhorse call per generation (review F1).

    Returns the stored summary unchanged when it's already cached in the target language, when
    there's nothing to translate, or when offline (no LLM) — the profile stays readable, just not
    re-localized. Legacy profiles with no cache are assumed to be in the default language."""
    native = taste.summary_text or ""
    cache = dict(taste.summary_by_lang or {})
    if not cache and native:
        cache = {DEFAULT_LANGUAGE: native}
    if language in cache:
        return cache[language]
    if not native or llm is None:
        return native
    prompt = (
        f"Translate the following taste summary into {LANGUAGE_NAMES[language]}. Keep it warm, "
        "concise, and addressed directly to the user in the second person. Output only the "
        f"translation, with no preamble.\n\n{native}"
    )
    try:
        translated = llm.complete(prompt, max_tokens=_TRANSLATE_MAX_TOKENS, temperature=0.0).strip()
    except Exception:  # noqa: BLE001 - a flaky provider must not break viewing the profile
        record_fallback("taste_summary_localization", "llm_error", language=language)
        return native
    if not translated:
        record_fallback("taste_summary_localization", "empty_completion", language=language)
        return native
    cache[language] = translated
    taste.summary_by_lang = cache
    session.commit()
    return translated


def optional_llm_provider() -> LLMProvider | None:
    """The configured LLM provider, or ``None`` when no key is set (offline mode)."""
    settings = get_settings()
    if not settings.llm_api_key:
        return None
    return OpenAILLMProvider(
        api_key=settings.llm_api_key,
        chat_model=settings.llm_chat_model,
        embedding_model=settings.llm_embedding_model,
        base_url=settings.llm_base_url,
        monthly_token_budget=settings.llm_monthly_token_budget,
        reasoning_headroom=settings.reasoning_headroom,
        timeout=settings.llm_timeout_seconds,
    )


def _new_events_since(session: Session, profile_id: uuid.UUID, since: datetime) -> int:
    """How many non-excluded events a profile has ingested since ``since`` — the change signal the
    auto-refresh gate weighs against the cost of a full re-extraction."""
    return (
        session.scalar(
            select(func.count())
            .select_from(WatchEvent)
            .where(
                WatchEvent.profile_id == profile_id,
                WatchEvent.excluded.is_(False),
                WatchEvent.ingested_at > since,
            )
        )
        or 0
    )


def _should_auto_refresh(session: Session, profile_id: uuid.UUID, now: datetime) -> bool:
    """Decide whether the *automatic* taste refresh is worth a workhorse call.

    Regenerating the whole profile because one episode synced is wasteful — a single event barely
    moves a 150-event taste read. So the auto-refresh fires only when the change is material:
    - no profile yet (or never generated) → always (the first extraction);
    - the current profile is ``degraded`` (the LLM extraction had failed, coarse fallback in place)
      → always, to re-attempt now the provider may have recovered (review A14);
    - at least ``taste_refresh_min_events`` new events since the last generation → enough drift to
      re-read now;
    - else, once the profile is older than ``taste_refresh_min_interval_seconds`` *and* something
      changed, fold the trickle in — but never re-run when nothing changed at all.

    The explicit ``POST /taste/generate`` bypasses this entirely (it calls ``generate`` directly).
    """
    settings = get_settings()
    taste = session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))
    if taste is None or taste.generated_at is None:
        return True
    if taste.degraded:
        # The current profile is the coarse fallback (extraction had failed) — re-attempt now that
        # we're here, in case the provider has recovered, regardless of how much history changed.
        return True
    new_events = _new_events_since(session, profile_id, taste.generated_at)
    if new_events >= settings.taste_refresh_min_events:
        return True
    if new_events == 0:
        return False
    age = (now - taste.generated_at).total_seconds()
    return age >= settings.taste_refresh_min_interval_seconds


def maybe_refresh_taste(
    session: Session,
    profile_id: uuid.UUID,
    llm: LLMProvider | None,
    language: Language = DEFAULT_LANGUAGE,
) -> bool:
    """Best-effort: regenerate a profile's taste from its history after its events change.

    Taste is a derived artifact (the agent's long-term memory), so it refreshes itself on ingest
    rather than waiting for a button. No-ops when no LLM is configured (the deterministic engine
    still personalises via the embedding centroid), and never lets a taste failure break ingestion.

    Gated for cost: a refresh only runs when the history changed materially (see
    :func:`_should_auto_refresh`) — so a stream of small incremental syncs doesn't fire a full
    re-extraction on every one. Returns ``True`` when a profile was (re)generated; ``False`` when
    skipped (offline, gated out, or failed). The caller owns the commit.
    """
    if llm is None:
        return False
    if not _should_auto_refresh(session, profile_id, datetime.now(UTC)):
        logger.debug("taste.auto_refresh_skipped", extra={"profile_id": str(profile_id)})
        return False
    try:
        TasteService(session, llm, get_settings().llm_chat_model, language).generate(profile_id)
        return True
    except Exception:  # noqa: BLE001 — taste is best-effort; ingestion must not fail on it
        logger.warning("taste.auto_refresh_failed", extra={"profile_id": str(profile_id)})
        return False
