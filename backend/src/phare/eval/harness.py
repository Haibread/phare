"""Run the engine over personas in a real DB session and score the slates.

Used by the CI guardrail suite and the ``phare eval`` CLI. With the local hash embedder this
runs entirely offline; pass a real embedder to evaluate the production embedding space.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.catalog.sample import seed_sample_catalog
from phare.db.models import EventType, Profile, Title, WatchEvent
from phare.eval import metrics
from phare.eval.personas import PERSONAS, Persona
from phare.providers.types import LLMProvider
from phare.recommend.schema import Recommendation
from phare.recommend.service import RecommendationService

logger = logging.getLogger(__name__)


@dataclass
class PersonaResult:
    name: str
    count: int
    forbidden_violations: list[str] = field(default_factory=list)
    recommended_watched: list[str] = field(default_factory=list)
    popularity_bias: float = 0.0
    intra_list_diversity: float = 0.0
    novelty: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.forbidden_violations and not self.recommended_watched


def _seed_persona_history(session: Session, profile_id: uuid.UUID, persona: Persona) -> None:
    seed_sample_catalog(session)
    for rank, (tmdb_id, rating) in enumerate(persona.watched):
        title = session.scalar(select(Title).where(Title.tmdb_id == tmdb_id))
        if title is None:  # pragma: no cover - personas reference real sample ids
            continue
        session.add(
            WatchEvent(
                profile_id=profile_id,
                title_id=title.id,
                type=EventType.rated,
                rating=rating,
                source="eval",
                external_ref=f"eval:{persona.name}:{rank}",
            )
        )
    session.flush()


def evaluate_persona(
    session: Session,
    persona: Persona,
    *,
    embed_provider: LLMProvider,
    model_version: str,
    k: int = 20,
) -> PersonaResult:
    """Run the engine for one persona and score the resulting slate."""
    profile = Profile(display_name=f"eval-{persona.name}")
    session.add(profile)
    session.flush()
    _seed_persona_history(session, profile.id, persona)

    service = RecommendationService(
        session, embed_provider=embed_provider, embed_model_version=model_version, chat_llm=None
    )
    service.ensure_embeddings()
    # Swing slots off: the guardrail measures the steered slate, not deliberate discovery.
    recs: list[Recommendation] = service.recommend(
        profile.id, taste=persona.taste, k=k, swing_slots=0
    )

    watched_ids = {
        title_id
        for title_id in session.scalars(
            select(WatchEvent.title_id).where(WatchEvent.profile_id == profile.id)
        )
    }
    forbidden = {g.lower() for g in persona.forbidden_genres}
    result = PersonaResult(name=persona.name, count=len(recs))
    for rec in recs:
        if forbidden & {g.lower() for g in rec.genres}:
            result.forbidden_violations.append(rec.title)
        if rec.title_id in watched_ids:
            result.recommended_watched.append(rec.title)

    pops = [_popularity(session, rec.title_id) for rec in recs]
    result.popularity_bias = metrics.popularity_bias(pops)
    result.novelty = metrics.novelty(pops)
    result.intra_list_diversity = metrics.intra_list_diversity([rec.genres for rec in recs])
    return result


def _popularity(session: Session, title_id: uuid.UUID) -> float | None:
    return session.scalar(select(Title.popularity).where(Title.id == title_id))


def evaluate_all(
    session: Session, *, embed_provider: LLMProvider, model_version: str, k: int = 20
) -> list[PersonaResult]:
    """Evaluate every persona. Each gets its own profile; isolation is preserved."""
    results = [
        evaluate_persona(
            session, persona, embed_provider=embed_provider, model_version=model_version, k=k
        )
        for persona in PERSONAS
    ]
    logger.info(
        "eval.done",
        extra={"personas": len(results), "passed": sum(r.passed for r in results)},
    )
    return results
