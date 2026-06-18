"""DB-backed engine tests: candidate generation, rows, and per-profile isolation.

Uses the local hash embedding provider so the real pgvector path runs with no credentials.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from phare.catalog.sample import seed_sample_catalog
from phare.db.models import Profile
from phare.ingest.sample import seed_sample_data
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.recommend.candidates import generate_candidates
from phare.recommend.rows import loved_seed_titles
from phare.recommend.service import RecommendationService
from phare.recommend.taste_vector import compute_taste_centroid, watched_title_ids


def _service(session: Session) -> RecommendationService:
    return RecommendationService(
        session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=None,
    )


def _seeded_profile(session: Session) -> uuid.UUID:
    profile = Profile(display_name="me")
    session.add(profile)
    session.flush()
    seed_sample_data(session, profile.id)
    seed_sample_catalog(session)
    session.flush()
    return profile.id


def test_candidates_exclude_watched(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    _service(db_session).ensure_embeddings()
    centroid = compute_taste_centroid(db_session, profile_id, LOCAL_MODEL_VERSION)
    assert centroid is not None

    watched = watched_title_ids(db_session, profile_id)
    candidates = generate_candidates(
        db_session, profile_id, centroid, LOCAL_MODEL_VERSION, limit=20
    )
    assert candidates  # the catalog gives us something to recommend
    assert all(c.title_id not in watched for c in candidates)


def test_candidates_respect_hard_avoids(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    _service(db_session).ensure_embeddings()
    centroid = compute_taste_centroid(db_session, profile_id, LOCAL_MODEL_VERSION)
    assert centroid is not None

    candidates = generate_candidates(
        db_session,
        profile_id,
        centroid,
        LOCAL_MODEL_VERSION,
        limit=50,
        hard_avoids=["Horror"],
    )
    assert candidates
    assert all("Horror" not in c.genres for c in candidates)


def test_rows_include_you_might_like_with_explanations(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    rows = _service(db_session).rows(profile_id)

    by_key = {row.key: row for row in rows}
    assert "you_might_like" in by_key
    yml = by_key["you_might_like"]
    assert yml.items
    assert all(item.explanation for item in yml.items)  # template fallback fills every one
    assert all(item.title_id not in watched_title_ids(db_session, profile_id) for item in yml.items)


def test_rows_have_watch_again_and_continue_watching(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    by_key = {row.key: row for row in _service(db_session).rows(profile_id)}

    # Sample history rates Dune 9 and Severance 10 -> watch_again; Severance has episode history.
    assert "watch_again" in by_key
    assert any(item.title == "Dune" for item in by_key["watch_again"].items)
    assert "continue_watching" in by_key
    assert any(item.title == "Severance" for item in by_key["continue_watching"].items)


def test_empty_profile_degrades_gracefully(db_session: Session) -> None:
    profile = Profile(display_name="newbie")
    db_session.add(profile)
    db_session.flush()
    seed_sample_catalog(db_session)

    rows = _service(db_session).rows(profile.id)
    # No history -> no centroid -> no you_might_like, no watch_again; but never an error.
    assert all(row.key != "you_might_like" for row in rows)


def test_recommendations_are_per_profile(db_session: Session) -> None:
    a = _seeded_profile(db_session)
    b_profile = Profile(display_name="other")
    db_session.add(b_profile)
    db_session.flush()
    # B has no history; A's watched titles must not leak into B's exclusion logic or vice versa.
    rows_a = _service(db_session).rows(a)
    assert rows_a  # A has recs
    assert watched_title_ids(db_session, b_profile.id) == set()  # isolation: B sees nothing of A


def test_loved_seed_titles_picks_loved(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    seeds = loved_seed_titles(db_session, profile_id, limit=3)
    assert seeds  # the sample history has highly-rated / loved titles
    assert len(seeds) <= 3
    assert len({s.id for s in seeds}) == len(seeds)  # distinct


def test_because_you_watched_rows_anchor_on_loved_titles(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    service = _service(db_session)
    service.ensure_embeddings()

    rows = service.because_you_watched_rows(profile_id, max_rows=3)

    assert rows
    assert all(row.key.startswith("because:") for row in rows)
    assert all(row.title.startswith("Because you watched ") for row in rows)
    watched = watched_title_ids(db_session, profile_id)
    for row in rows:
        assert row.items
        # Picks never include watched titles (so the seed itself can't recur).
        assert all(item.title_id not in watched for item in row.items)


def test_because_rows_render_before_you_might_like(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    keys = [row.key for row in _service(db_session).rows(profile_id)]

    because_idx = next((i for i, k in enumerate(keys) if k.startswith("because:")), None)
    assert because_idx is not None
    if "you_might_like" in keys:
        assert because_idx < keys.index("you_might_like")  # most-personalized first
