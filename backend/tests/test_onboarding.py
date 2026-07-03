"""M4.3 [C3]: the sample-onboarding path exposes ordered readiness and lands fast.

The taste extraction (the slow LLM pass) moves to a background refresh, so the user reaches Browse
as soon as the catalog + history exist. Onboarding status is derived from real DB state.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import phare.taste.backfill as taste_backfill
from phare.api.deps import Embedder, get_embedder, get_optional_chat_llm
from phare.catalog.sample import seed_sample_catalog
from phare.db.models import TasteProfile, User
from phare.ingest.sample import seed_sample_data
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider
from phare.taste.backfill import schedule_taste_refresh
from tests.conftest import authed_client, make_account


@pytest.fixture(autouse=True)
def _reset_taste_running() -> None:
    """Clear the per-profile "refresh in flight" set — a test that parks a runner would otherwise
    leak a stuck profile id into the next test."""
    yield
    with taste_backfill._lock:
        taste_backfill._running.clear()


def _client(session: Session, user: User) -> TestClient:
    overrides = {
        get_embedder: lambda: Embedder(
            provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
        ),
        get_optional_chat_llm: lambda: None,
    }
    return authed_client(session, user, overrides=overrides)


def _status(client: TestClient, profile_id: str) -> dict:
    return client.get(f"/profiles/{profile_id}/onboarding").json()


def test_onboarding_status_advances_through_the_steps_in_order(db_session: Session) -> None:
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = str(user.profile.id)

    # Nothing seeded yet — every step pending.
    start = _status(client, profile_id)
    assert start == {
        "catalogReady": False,
        "historyReady": False,
        "tasteReady": False,
        "readyToBrowse": False,
    }

    # Catalog first.
    client.post("/catalog/sample")
    after_catalog = _status(client, profile_id)
    assert after_catalog["catalogReady"] is True
    assert after_catalog["historyReady"] is False
    assert after_catalog["readyToBrowse"] is False

    # Then history — which is enough to browse (taste, offline, collapses onto history).
    client.post(f"/profiles/{profile_id}/sample-data")
    after_history = _status(client, profile_id)
    assert after_history["catalogReady"] is True
    assert after_history["historyReady"] is True
    assert after_history["readyToBrowse"] is True


def test_browse_is_reachable_before_taste_extraction_finishes(
    db_session: Session, monkeypatch
) -> None:
    # With an LLM configured, taste extraction is a *separate* background step: the catalog and
    # history are ready (so the app can land) while the taste profile isn't written yet.
    monkeypatch.setattr("phare.api.profiles.optional_llm_provider", lambda: FakeLLMProvider())
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = str(user.profile.id)
    seed_sample_catalog(db_session)
    seed_sample_data(db_session, user.profile.id)
    db_session.flush()

    mid = _status(client, profile_id)
    assert mid["readyToBrowse"] is True  # catalog + history ready → land now
    assert mid["tasteReady"] is False  # taste still extracting in the background

    # Once the background refresh has written the profile, the last step flips.
    db_session.add(TasteProfile(profile_id=user.profile.id, summary_text="likes sci-fi"))
    db_session.flush()
    done = _status(client, profile_id)
    assert done["tasteReady"] is True


def test_schedule_taste_refresh_is_one_per_profile_and_offline_noop() -> None:
    profile_id = uuid.uuid4()
    parked: list[object] = []

    def parking_runner(work: object) -> None:
        parked.append(work)  # hold it in flight

    # Offline (no LLM): nothing to extract.
    assert schedule_taste_refresh(profile_id, None, "model", "en", runner=parking_runner) is False
    assert not parked

    llm = FakeLLMProvider()
    first = schedule_taste_refresh(profile_id, llm, "model", "en", runner=parking_runner)
    second = schedule_taste_refresh(profile_id, llm, "model", "en", runner=parking_runner)
    assert first is True and second is False  # per-profile lock dedupes the second
    assert len(parked) == 1

    # A different profile is independent (not blocked by the first).
    other = schedule_taste_refresh(uuid.uuid4(), llm, "model", "en", runner=parking_runner)
    assert other is True


def test_sample_data_returns_immediately_offline(db_session: Session) -> None:
    # Offline, there's no taste to extract, so the seed reports no background work and just lands.
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = str(user.profile.id)
    client.post("/catalog/sample")

    body = client.post(f"/profiles/{profile_id}/sample-data").json()

    assert body["created"] > 0
    assert body["tasteBuilding"] is False
