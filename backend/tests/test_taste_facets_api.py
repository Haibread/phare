"""API tests for the read-only taste-facets endpoint (GET /profiles/{id}/taste/facets).

The facet clustering itself is covered in ``test_taste_facets.py``; these drive the HTTP surface:
profile isolation, the empty-list contract for single-mode/empty histories, and the genre labels +
exemplars a real two-mode history yields. Embeddings are hand-built (local tag) so the geometry is
deterministic — no LLM, no network.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from phare.db.models import (
    EMBEDDING_DIM,
    EventType,
    Title,
    TitleEmbedding,
    TitleKind,
    User,
    WatchEvent,
)
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION
from tests.conftest import authed_client, make_account


def _mode_vector(lo: int, hi: int, jitter: int) -> list[float]:
    """A vector living in dims [lo, hi) with per-title jitter — same trick as the unit tests, so
    members of a mode are similar but not identical and the two modes are orthogonal."""
    out = [0.0] * EMBEDDING_DIM
    for j in range(lo, hi):
        out[j] = 1.0 + 0.01 * ((j + jitter) % 3)
    return out


def _add_watched_title(
    session: Session,
    profile_id: uuid.UUID,
    *,
    tmdb_id: int,
    name: str,
    genres: list[str],
    vector: list[float],
) -> Title:
    title = Title(
        kind=TitleKind.movie,
        tmdb_id=tmdb_id,
        title=name,
        year=2000 + tmdb_id % 30,
        genres=genres,
        poster_path=f"/poster-{tmdb_id}.jpg",
    )
    session.add(title)
    session.flush()
    session.add(
        TitleEmbedding(title_id=title.id, model_version=LOCAL_MODEL_VERSION, embedding=vector)
    )
    session.add(
        WatchEvent(profile_id=profile_id, title_id=title.id, type=EventType.watched, source="test")
    )
    return title


def _two_mode_history(session: Session, user: User) -> tuple[list[Title], list[Title]]:
    """Six sci-fi titles in one embedding mode + six comedies in an orthogonal one — enough
    positives to split, distinct enough to yield exactly two facets."""
    scifi = [
        _add_watched_title(
            session,
            user.profile.id,
            tmdb_id=100 + i,
            name=f"Sci-Fi {i}",
            # Thriller on a third of the members: below the co-dominance bar, so the label
            # stays the single dominant genre.
            genres=["Science Fiction", "Thriller"] if i < 2 else ["Science Fiction"],
            vector=_mode_vector(0, 4, i),
        )
        for i in range(6)
    ]
    comedy = [
        _add_watched_title(
            session,
            user.profile.id,
            tmdb_id=200 + i,
            name=f"Comedy {i}",
            genres=["Comedy"],
            vector=_mode_vector(4, 8, i),
        )
        for i in range(6)
    ]
    session.flush()
    return scifi, comedy


def test_facets_empty_history_returns_empty_list(db_session: Session) -> None:
    user = make_account(db_session)
    response = authed_client(db_session, user).get(f"/profiles/{user.profile.id}/taste/facets")
    assert response.status_code == 200
    assert response.json() == {"facets": []}


def test_facets_single_mode_history_returns_empty_list(db_session: Session) -> None:
    # A cohesive taste (every title in one embedding mode) collapses to k=1 — one blob facet is
    # "your taste", not an insight, so the API returns nothing to render.
    user = make_account(db_session)
    for i in range(10):
        _add_watched_title(
            db_session,
            user.profile.id,
            tmdb_id=300 + i,
            name=f"Mono {i}",
            genres=["Drama"],
            vector=_mode_vector(0, 4, i),
        )
    db_session.flush()
    response = authed_client(db_session, user).get(f"/profiles/{user.profile.id}/taste/facets")
    assert response.status_code == 200
    assert response.json() == {"facets": []}


def test_facets_two_mode_history_yields_labeled_facets_with_exemplars(
    db_session: Session,
) -> None:
    user = make_account(db_session)
    scifi, comedy = _two_mode_history(db_session, user)

    response = authed_client(db_session, user).get(f"/profiles/{user.profile.id}/taste/facets")
    assert response.status_code == 200
    facets = response.json()["facets"]
    assert len(facets) == 2

    # Deterministic genre labels: the dominant genre of each mode's members, English catalog terms
    # (the client localises). Thriller appears on only 2/6 sci-fi members — not co-dominant.
    assert {facet["label"] for facet in facets} == {"Science Fiction", "Comedy"}
    # Weights are shares summing to 1, served descending.
    assert abs(sum(facet["weight"] for facet in facets) - 1.0) < 1e-9
    assert facets[0]["weight"] >= facets[1]["weight"]

    scifi_ids = {str(t.id) for t in scifi}
    comedy_ids = {str(t.id) for t in comedy}
    for facet in facets:
        assert facet["titleCount"] == 6
        exemplars = facet["exemplars"]
        assert len(exemplars) == 3
        member_pool = scifi_ids if facet["label"] == "Science Fiction" else comedy_ids
        for exemplar in exemplars:
            # Exemplars come from the facet's own members, fully serialised (camelCase wire).
            assert exemplar["titleId"] in member_pool
            assert exemplar["title"]
            assert exemplar["year"] is not None
            assert exemplar["posterUrl"] is not None and "/poster-" in exemplar["posterUrl"]


def test_facets_are_deterministic_across_requests(db_session: Session) -> None:
    user = make_account(db_session)
    _two_mode_history(db_session, user)
    client = authed_client(db_session, user)
    first = client.get(f"/profiles/{user.profile.id}/taste/facets").json()
    second = client.get(f"/profiles/{user.profile.id}/taste/facets").json()
    assert first == second


def test_facets_are_profile_isolated(db_session: Session) -> None:
    owner = make_account(db_session)
    _two_mode_history(db_session, owner)
    intruder = make_account(db_session, display_name="other")
    # 404 (not 403): a probe must not learn the profile exists (see docs/auth.md).
    response = authed_client(db_session, intruder).get(f"/profiles/{owner.profile.id}/taste/facets")
    assert response.status_code == 404
