"""Candidate generation: vector search over the catalog, minus watched and hard-avoids.

"LLM steers, embeddings rank" — this is the rank half. pgvector finds the titles nearest the
taste centroid; hard filters remove what the profile has seen and what the taste profile marks
as a hard avoid. Scoring/steering happens later in the re-ranker.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import and_, func, not_, or_, select, text, true
from sqlalchemy.orm import Session

from phare.core.fallback import record_fallback
from phare.db.models import Title, TitleEmbedding
from phare.recommend import genres
from phare.recommend.schema import Candidate
from phare.recommend.taste_vector import watched_title_ids

logger = logging.getLogger(__name__)


def _is_hard_avoided(title: Title, avoids: Sequence[str]) -> bool:
    """A title is avoided if any hard-avoid term matches its title, a genre, or a keyword.

    Uses the shared genre-match rule (:mod:`phare.recommend.genres`): alias-resolved equality, or a
    substring of length >= 4 either way. So a free avoid like "Mindless franchise sequels" catches a
    title tagged "franchise" (substring), while "war" still won't drop "Warrior" (< 4 → equality
    required) — the same rule the taste affinities score by (review H1).
    """
    tokens = [title.title, *title.genres, *title.keywords]
    return any(genres.matches_any((avoid,), tokens) for avoid in avoids if avoid.strip())


def _avoided_genre_labels(session: Session, hard_avoids: Sequence[str]) -> list[str]:
    """The catalog genre labels the hard-avoid terms resolve to — the SQL-enforceable slice of the
    avoids (measured live: a mood query's entire ANN pool was genre-avoided and dropped in Python,
    wasting the whole fetch). Resolved against the catalog vocabulary with the same alias/substring
    rule the in-memory filter uses, so the SQL exclusion never drops more than ``_is_hard_avoided``
    would. Free-form avoids matching no catalog label resolve to nothing and don't constrain SQL —
    the Python backstop (which also matches title text and keywords) still catches them."""
    terms = [a for a in hard_avoids if a.strip()]
    if not terms:
        return []
    vocabulary = session.execute(select(func.distinct(func.unnest(Title.genres)))).scalars()
    return genres.resolve_catalog_genres(terms, (g for g in vocabulary if g))


def generate_candidates(
    session: Session,
    profile_id: uuid.UUID,
    centroid: Sequence[float],
    model_version: str,
    *,
    limit: int = 50,
    hard_avoids: Sequence[str] = (),
    from_watched: bool = False,
    genre_constraints: Sequence[genres.GenreConstraint] = (),
    max_runtime: int | None = None,
) -> list[Candidate]:
    """Nearest titles to the centroid, hard-avoids removed.

    Hard-avoids are enforced twice: the terms that resolve to catalog genre labels are excluded in
    the ANN SQL (``NOT (genres && avoided_labels)``) so the fetched pool isn't spent on titles that
    would be dropped wholesale, and the in-memory ``_is_hard_avoided`` backstop then catches the
    title-text/keyword matches SQL can't express.

    Default: catalog titles the profile has *not* watched (find something new). With
    ``from_watched=True`` the source flips to titles the profile *has* watched — the candidate pool
    for a rewatch, ranked the same way (nearest the taste centroid).

    ``genre_constraints`` / ``max_runtime`` push a constraint into SQL so the ANN searches the
    *right* subspace instead of the taste-nearest slice that may contain few matches (round-7
    finding 1). Each :class:`genres.GenreConstraint` carries canonical catalog genre labels
    (already alias-resolved by the caller — see :func:`genres.resolve_catalog_constraints`) and an
    optional origin language ("anime" → Animation + ``ja``): a title matches a constraint when its
    ``genres`` array overlaps the labels AND (when set) its ``original_language`` equals the
    required origin; it matches the set when it matches any one (OR — same semantics as the
    in-memory filter). A ``max_runtime`` keeps titles at/under the cap **or** with an unknown
    (NULL) runtime — the runtime heal runs on the retrieved pool afterwards, so a still-NULL row is
    a candidate the cap can't yet judge, not one to exclude. Ranking (distance to centroid + stable
    tie-break) is unchanged, so the filtered result is the true nearest matches, deterministic
    under wide search.
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
    constraints = [scope]
    # Hard-avoids that resolve to catalog genre labels are excluded in the ANN SQL itself, so the
    # fetched pool isn't wasted on titles the avoid filter would drop wholesale (a genre-heavy avoid
    # used to empty the entire pool post-hoc — ``hard_avoids_emptied``). The in-memory
    # ``_is_hard_avoided`` check below stays as the backstop: it also matches title text and
    # keywords, which SQL doesn't see.
    if avoided_labels := _avoided_genre_labels(session, hard_avoids):
        constraints.append(not_(Title.genres.op("&&")(avoided_labels)))
    if genre_constraints:
        # Array overlap (``&&``) per constraint, plus the origin-language equality when the
        # constraint carries one; OR across constraints. Labels are canonical, so this honours the
        # same alias semantics as ``genres.matches_any``.
        clauses = []
        for gc in genre_constraints:
            clause = Title.genres.op("&&")(list(gc.labels))
            if gc.original_language is not None:
                clause = and_(clause, Title.original_language == gc.original_language)
            clauses.append(clause)
        constraints.append(or_(*clauses))
    if max_runtime is not None:
        constraints.append(
            or_(Title.runtime_minutes.is_(None), Title.runtime_minutes <= max_runtime)
        )
    distance = TitleEmbedding.embedding.cosine_distance(list(centroid))
    # Over-fetch so the post-filter for hard-avoids can't starve the result below ``limit``.
    pool = limit * 3 + len(hard_avoids) + 10
    # The re-ranker must be deterministic, but the ``ix_title_embedding_hnsw`` index is an
    # *approximate* nearest-neighbour structure: at the default ``ef_search`` the same query can
    # return a slightly different set near the cutoff as the graph changes, flickering the rows a
    # user sees on reload. Widen the search beam so recall is effectively exact for a self-hosted
    # catalog (works at N=200, principle 5); ``SET LOCAL`` scopes it to this transaction.
    session.execute(text("SET LOCAL hnsw.ef_search = 1000"))
    rows = session.execute(
        select(Title, distance.label("distance"))
        .join(TitleEmbedding, TitleEmbedding.title_id == Title.id)
        .where(
            TitleEmbedding.model_version == model_version,
            *constraints,
        )
        # Break exact-distance ties on the stable catalog id (``tmdb_id``, not the per-insert
        # random UUID) so the ``limit(pool)`` cutoff resolves identically on every run and process.
        .order_by(distance.asc(), Title.tmdb_id.asc().nulls_last(), Title.id)
        .limit(pool)
    ).all()

    candidates: list[Candidate] = []
    dropped_by_avoids = 0
    for title, dist in rows:
        if _is_hard_avoided(title, hard_avoids):
            dropped_by_avoids += 1
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
                original_language=title.original_language,
                overview=title.overview,
                poster_path=title.poster_path,
                similarity=1.0 - float(dist),
            )
        )
        if len(candidates) >= limit:
            break
    # Hard-avoids emptied the pool (nearby titles existed, the avoids removed them all) — a
    # thin/degraded slate the operator should see, not a mysteriously empty row (review A15/G1).
    if not candidates and dropped_by_avoids:
        record_fallback(
            "candidates", "hard_avoids_emptied", dropped=dropped_by_avoids, avoids=len(hard_avoids)
        )
    logger.debug(
        "recommend.candidates",
        extra={"profile_id": str(profile_id), "candidate_count": len(candidates)},
    )
    return candidates
