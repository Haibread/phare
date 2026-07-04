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
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION
from phare.providers.types import LLMProvider
from phare.recommend import genres
from phare.recommend.schema import Recommendation
from phare.recommend.service import RecommendationService

logger = logging.getLogger(__name__)

# Alignment thresholds (mission M10.2). These port the pytest lot-1 alignment invariants into the
# harness, so `phare evaluate` — which runs against the *deployed* instance with the real embedding
# model — catches a relevance regression the hermetic CI (fake providers) structurally can't see.
#
# ``similarity_rel`` spread: a healthy slate places its picks across the pool-relative scale rather
# than reading "strong fit" for everything (review H2/A8). We require the top-K to span at least
# this much. The offline local-hash embedder is not the production space, so this spread check is
# skipped (and said so in the output) when the model is ``local-hash-v1`` — the CI harness runs on
# it and it must stay green; the real deployed model is what this check is really guarding.
_MIN_SIM_REL_SPREAD = 0.15
# Affinity variance: a slate whose affinity column is flat *at the neutral value* is the H1 bug —
# the taste key matched no candidate genre, so every candidate scored neutral. A column flat at any
# *non-neutral* value is the opposite (the key matched everything: a perfectly on-genre slate), the
# goal rather than a bug. score_candidate maps a net affinity of 0 to (0 + 1) / 2 = 0.5, so that's
# the neutral point the check keys on.
_NEUTRAL_AFFINITY = 0.5


@dataclass
class PersonaResult:
    name: str
    count: int
    forbidden_violations: list[str] = field(default_factory=list)
    recommended_watched: list[str] = field(default_factory=list)
    alignment_failures: list[str] = field(default_factory=list)
    popularity_bias: float = 0.0
    intra_list_diversity: float = 0.0
    novelty: float = 0.0

    @property
    def passed(self) -> bool:
        return (
            not self.forbidden_violations
            and not self.recommended_watched
            and not self.alignment_failures
        )


def alignment_checks_summary(model_version: str) -> str:
    """One line naming the alignment checks the harness runs, for the ``phare evaluate`` header.

    Names the similarity-spread check as *skipped* on the offline embedder so a green run is never
    read as having exercised it (mission M10.2). Empty slates are likewise not failed offline (the
    local-hash embedder leaves several personas with no neighbours) — flagged here for the same
    reason."""
    if model_version == LOCAL_MODEL_VERSION:
        spread = "similarity-spread + empty-slate [skipped: offline local-hash embedder]"
    else:
        spread = f"similarity-spread (>= {_MIN_SIM_REL_SPREAD})"
    return f"hard-avoids, affinity-variance, score-order, {spread}"


def _alignment_failures(
    recs: list[Recommendation], persona: Persona, *, model_version: str
) -> list[str]:
    """Check the slate against the taste-alignment invariants (M10.2), returning a reason per
    failed check. Reuses the shared genre matcher (:func:`phare.recommend.genres.matches_any`) for
    hard-avoids — no second matcher. The similarity-spread *and* empty-slate checks are relaxed on
    the offline local-hash embedder (not the production space); every other check runs on any model.
    """
    offline = model_version == LOCAL_MODEL_VERSION
    failures: list[str] = []
    if not recs:
        # The local-hash embedder is not the production space: several personas' taste centroids
        # have no meaningful neighbours in it, so an empty slate offline is an embedder artifact,
        # not an engine regression — don't fail the guardrail on it (the header names this skip via
        # ``alignment_checks_summary``). On the real embedding space an empty slate is a genuine
        # alignment failure. Same rule as the similarity-spread check below.
        if offline:
            return []
        return ["empty slate: the engine returned nothing to align"]

    # (a) No hard-avoid the persona's taste marks may appear in the top-K.
    avoids = [a for a in (persona.taste.get("hard_avoids") or []) if str(a).strip()]
    leaked = [rec.title for rec in recs if genres.matches_any(avoids, [rec.title, *rec.genres])]
    if leaked:
        failures.append(f"hard-avoid leaked into top-K: {', '.join(leaked)}")

    # (b) Affinity must not be flat at the *neutral* value — that's the H1 bug signature: the taste
    # key matched no candidate genre, so every candidate scored the neutral 0.5. A slate flat at a
    # *non-neutral* value is the opposite: the key matched everything (a perfectly on-genre slate,
    # e.g. a comedy-only taste over a comedy-heavy pool) — that's the goal, not a bug, so it passes.
    # (Was: any flat column failed, which false-flagged a well-aligned slate once the relevance
    # floor trimmed its off-genre tail — a comedy-only slate at uniform high affinity is fine.)
    if persona.taste.get("affinities"):
        distinct_affinity = {rec.components.get("affinity") for rec in recs}
        if distinct_affinity == {_NEUTRAL_AFFINITY}:
            failures.append(
                f"affinity is flat at the neutral {_NEUTRAL_AFFINITY}: "
                "the taste key matched no candidate genre"
            )

    # (c) The slate must read score-descending — a reintroduced vote-count sort breaks this (A1/H7).
    scores = [rec.score for rec in recs]
    if scores != sorted(scores, reverse=True):
        failures.append("slate is not ordered by descending score")

    # (d) Pool-relative similarity must spread, not read "strong fit" for everything (H2/A8).
    # Skipped on the offline embedder (not the production space) — reported so a green run is not
    # mistaken for having exercised this check.
    sim_rels = [rec.components.get("similarity_rel", 0.5) for rec in recs]
    if model_version != LOCAL_MODEL_VERSION:
        spread = max(sim_rels) - min(sim_rels)
        if spread < _MIN_SIM_REL_SPREAD:
            failures.append(
                f"similarity_rel is flat (spread {spread:.3f} < {_MIN_SIM_REL_SPREAD}): "
                "the slate reads 'strong fit' for everything"
            )
    return failures


def _seed_persona_history(session: Session, profile_id: uuid.UUID, persona: Persona) -> None:
    seed_sample_catalog(session)
    for rank, (tmdb_id, rating) in enumerate(persona.watched):
        # Personas reference movie tmdb_ids in the fixed sample catalog (no movie/show id collision
        # there), so a kind-less lookup is unambiguous here — unlike the ingest/catalog paths (H3a).
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
    # Alignment (M10.2) is checked on the score-ordered chat slate (``vote_mix``), not the MMR-
    # diversified rows slate above — MMR reorders for genre variety, so a "score-descending" check
    # only reads honestly on the score-ranked composition. Deterministic, no LLM (chat_llm=None).
    aligned: list[Recommendation] = service.recommend(
        profile.id, taste=persona.taste, k=k, swing_slots=0, vote_mix=True
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
    result.alignment_failures = _alignment_failures(aligned, persona, model_version=model_version)

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
