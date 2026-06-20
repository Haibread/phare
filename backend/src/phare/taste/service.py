"""Taste extraction: turn a profile's events into a structured, editable taste profile.

The LLM reads the fuzzy signal (ratings, abandonment, rewatches, genres) and emits a
validated structured profile. User edits live in ``user_overrides`` and always win.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.core.config import get_settings
from phare.db.models import TasteProfile, Title, WatchEvent
from phare.providers.llm import OpenAILLMProvider
from phare.providers.types import LLMProvider
from phare.taste.schema import TasteProfileData

logger = logging.getLogger(__name__)

_MAX_EVENTS = 200

_PROMPT_HEADER = """You analyze a viewer's watch history and produce a structured taste profile.

Output ONLY a JSON object with these keys:
- summary: one short paragraph describing their taste, in plain language
- likes: array of strings (what they gravitate toward)
- dislikes: array of strings
- hard_avoids: array of strings (never recommend these)
- affinities: object mapping genre/keyword/era/tone -> weight in [-1, 1]
- comfort_axis: short string for their comfort/rewatch leanings (or null)
- discovery_tolerance: number in [0, 1] (appetite for unfamiliar picks)
- confidence: number in [0, 1] based on how much history is available

Guidance: weight recent activity more; treat abandoned/low-rated titles as strong negative
signal and rewatches as comfort signal. Be specific. History (most recent first):
"""


def effective_profile(taste: TasteProfile) -> dict[str, Any]:
    """The structured profile with sticky user edits applied on top."""
    merged = dict(taste.structured)
    merged.update(taste.user_overrides)
    return merged


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[len("json") :]
    parsed: dict[str, Any] = json.loads(text.strip())
    return parsed


class TasteService:
    """Generates and persists a profile's taste profile via the LLM."""

    def __init__(self, session: Session, llm: LLMProvider, model_version: str) -> None:
        self.session = session
        self.llm = llm
        self.model_version = model_version

    def _history_lines(self, profile_id: uuid.UUID) -> list[str]:
        rows = self.session.execute(
            select(WatchEvent, Title)
            .join(Title, WatchEvent.title_id == Title.id)
            .where(WatchEvent.profile_id == profile_id, WatchEvent.excluded.is_(False))
            .order_by(WatchEvent.occurred_at.desc().nulls_last())
            .limit(_MAX_EVENTS)
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

    def generate(self, profile_id: uuid.UUID) -> TasteProfile:
        lines = self._history_lines(profile_id)
        history = "\n".join(lines) if lines else "(no history)"
        prompt = _PROMPT_HEADER + history + self._memory_block(profile_id)
        logger.info("taste.generate", extra={"profile_id": str(profile_id), "events": len(lines)})

        raw = self.llm.complete(prompt)
        data = TasteProfileData.model_validate(_extract_json(raw))

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
    )


def maybe_refresh_taste(session: Session, profile_id: uuid.UUID, llm: LLMProvider | None) -> bool:
    """Best-effort: regenerate a profile's taste from its history after its events change.

    Taste is a derived artifact (the agent's long-term memory), so it refreshes itself on ingest
    rather than waiting for a button. No-ops when no LLM is configured (the deterministic engine
    still personalises via the embedding centroid), and never lets a taste failure break ingestion.
    Returns ``True`` when a profile was (re)generated. The caller owns the commit.
    """
    if llm is None:
        return False
    try:
        TasteService(session, llm, get_settings().llm_chat_model).generate(profile_id)
        return True
    except Exception:  # noqa: BLE001 — taste is best-effort; ingestion must not fail on it
        logger.warning("taste.auto_refresh_failed", extra={"profile_id": str(profile_id)})
        return False
