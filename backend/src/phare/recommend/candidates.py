"""Candidate generation: vector search over the catalog, minus watched and hard-avoids.

"LLM steers, embeddings rank" — this is the rank half. pgvector finds the titles nearest the
taste centroid; hard filters remove what the profile has seen and what the taste profile marks
as a hard avoid. Scoring/steering happens later in the re-ranker.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from phare.db.models import Title, TitleEmbedding
from phare.recommend.schema import Candidate
from phare.recommend.taste_vector import watched_title_ids

logger = logging.getLogger(__name__)


def _is_hard_avoided(title: Title, avoids: Sequence[str]) -> bool:
    """A title is avoided if any hard-avoid term matches its title, a genre, or a keyword.

    Matched on word boundaries, not raw substrings: avoiding "war" must not also drop "Warrior"
    or "Steward". Multi-word terms ("true crime") match as a phrase.
    """
    haystack = {
        title.title.lower(),
        *(g.lower() for g in title.genres),
        *(k.lower() for k in title.keywords),
    }
    for avoid in avoids:
        needle = avoid.strip().lower()
        if not needle:
            continue
        pattern = re.compile(rf"\b{re.escape(needle)}\b")
        if any(pattern.search(text) for text in haystack):
            return True
    return False


def generate_candidates(
    session: Session,
    profile_id: uuid.UUID,
    centroid: Sequence[float],
    model_version: str,
    *,
    limit: int = 50,
    hard_avoids: Sequence[str] = (),
    from_watched: bool = False,
) -> list[Candidate]:
    """Nearest titles to the centroid, hard-avoids removed.

    Default: catalog titles the profile has *not* watched (find something new). With
    ``from_watched=True`` the source flips to titles the profile *has* watched — the candidate pool
    for a rewatch, ranked the same way (nearest the taste centroid).
    """
    watched = watched_title_ids(session, profile_id)
    if from_watched and not watched:
        return []  # nothing watched yet → nothing to rewatch
    if from_watched:
        scope = Title.id.in_(watched)
    elif watched:
        scope = Title.id.notin_(watched)
    else:
        scope = true()
    distance = TitleEmbedding.embedding.cosine_distance(list(centroid))
    # Over-fetch so the post-filter for hard-avoids can't starve the result below ``limit``.
    pool = limit * 3 + len(hard_avoids) + 10
    rows = session.execute(
        select(Title, distance.label("distance"))
        .join(TitleEmbedding, TitleEmbedding.title_id == Title.id)
        .where(
            TitleEmbedding.model_version == model_version,
            scope,
        )
        .order_by(distance.asc())
        .limit(pool)
    ).all()

    candidates: list[Candidate] = []
    for title, dist in rows:
        if _is_hard_avoided(title, hard_avoids):
            continue
        candidates.append(
            Candidate(
                title_id=title.id,
                title=title.title,
                kind=title.kind.value,
                year=title.year,
                genres=list(title.genres),
                keywords=list(title.keywords),
                runtime_minutes=title.runtime_minutes,
                popularity=title.popularity,
                vote_count=title.vote_count,
                vote_average=title.vote_average,
                overview=title.overview,
                poster_path=title.poster_path,
                similarity=1.0 - float(dist),
            )
        )
        if len(candidates) >= limit:
            break
    logger.debug(
        "recommend.candidates",
        extra={"profile_id": str(profile_id), "candidate_count": len(candidates)},
    )
    return candidates
