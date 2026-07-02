"""Taste extraction: turn a profile's events into a structured, editable taste profile.

The LLM reads the fuzzy signal (ratings, abandonment, rewatches, genres) and emits a
validated structured profile. User edits live in ``user_overrides`` and always win.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.core.config import get_settings
from phare.core.fallback import record_fallback
from phare.core.i18n import DEFAULT_LANGUAGE, LANGUAGE_NAMES, Language, translate
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

_ALLOWED_GENRES = ", ".join(genres.CANONICAL_GENRES)
_ALLOWED_KEYWORDS = ", ".join(genres.AFFINITY_KEYWORDS)

# ``affinities`` and ``hard_avoids`` are the keys that actually steer scoring, so they must line up
# with what catalog titles are tagged with — a free key ("Mind-bending Sci-Fi") matched nothing and
# left the profile inert (review H1). Constrain them to a closed vocabulary; ``summary`` / ``likes``
# / ``dislikes`` stay free-form natural language so the displayed profile isn't impoverished.
_PROMPT_HEADER = f"""You analyze a viewer's watch history and produce a structured taste profile.

Output ONLY a JSON object with these keys:
- summary: one short paragraph describing their taste, in plain language
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
- genres: {_ALLOWED_GENRES}
- descriptors: {_ALLOWED_KEYWORDS}

Guidance: weight recent activity more; treat abandoned/low-rated titles as strong negative
signal and rewatches as comfort signal. Be specific. History (most recent first):
"""


def effective_profile(taste: TasteProfile) -> dict[str, Any]:
    """The structured profile with sticky user edits applied on top."""
    merged = dict(taste.structured)
    merged.update(taste.user_overrides)
    return merged


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
        rows = self.session.execute(
            select(WatchEvent, Title)
            .join(Title, WatchEvent.title_id == Title.id)
            .where(WatchEvent.profile_id == profile_id, WatchEvent.excluded.is_(False))
            .order_by(WatchEvent.occurred_at.desc().nulls_last())
            .limit(get_settings().taste_max_events)
        ).all()
        lines: list[str] = []
        for event, title in rows:
            genres = ", ".join(title.genres) if title.genres else "?"
            rating = f" rating={float(event.rating):g}" if event.rating is not None else ""
            lines.append(
                f"- [{event.type.value}] {title.title} ({title.kind.value}, "
                f"{title.year or '?'}) genres: {genres}{rating}"
            )
        return lines

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
        prompt = _PROMPT_HEADER + history + self._memory_block(profile_id)
        if self.language != DEFAULT_LANGUAGE:
            # Only the human-readable summary localises; structured keys stay English because they
            # key affinity matching against catalog genre names.
            prompt += (
                f"\n\nWrite the `summary` field in {LANGUAGE_NAMES[self.language]}. Keep every "
                "other string value (likes, dislikes, hard_avoids, affinity keys) in English."
            )
        logger.info("taste.generate", extra={"profile_id": str(profile_id), "events": len(lines)})

        # temperature=0: taste extraction is structured JSON, not prose — the same history should
        # always distil to the same profile (and the same parse), so don't let sampling wobble it.
        raw = self.llm.complete(prompt, max_tokens=_TASTE_MAX_TOKENS, temperature=0.0)
        try:
            data = TasteProfileData.model_validate(extract_json(raw))
        except (ValueError, ValidationError):
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
        taste.structured = data.model_dump(mode="json")
        taste.confidence = data.confidence
        taste.model_version = self.model_version
        taste.generated_at = datetime.now(UTC)
        # user_overrides intentionally left untouched — hand edits survive regeneration.
        self.session.flush()
        return taste


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
        reasoning_headroom=settings.reasoning_headroom,
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
