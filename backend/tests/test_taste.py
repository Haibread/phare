"""Taste extraction service + API (generate, view, edit, override persistence)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.api.taste import get_llm_provider
from phare.db.base import get_session
from phare.db.models import Profile, TasteProfile
from phare.ingest.sample import seed_sample_data
from phare.providers.fakes import FakeLLMProvider
from phare.taste.service import (
    TasteService,
    effective_profile,
    maybe_refresh_taste,
    optional_llm_provider,
)

CANNED = (
    '{"summary":"Likes cerebral sci-fi and prestige drama.",'
    '"likes":["slow-burn sci-fi"],"dislikes":["slapstick"],'
    '"hard_avoids":["gore"],"affinities":{"Science Fiction":0.9},'
    '"comfort_axis":"prestige drama","discovery_tolerance":0.6,"confidence":0.7}'
)


def _profile_with_history(session: Session) -> uuid.UUID:
    profile = Profile(display_name="me")
    session.add(profile)
    session.flush()
    seed_sample_data(session, profile.id)
    session.flush()
    return profile.id


def test_generate_builds_profile(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=CANNED)

    taste = TasteService(db_session, llm, "test-model").generate(profile_id)

    assert taste.summary_text is not None
    assert taste.structured["affinities"]["Science Fiction"] == 0.9
    assert taste.confidence == 0.7
    assert taste.generated_at is not None
    # The prompt must actually include the user's history.
    assert "Dune" in llm.prompts[0]


def test_generate_preserves_user_overrides(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    service = TasteService(db_session, FakeLLMProvider(completion=CANNED), "m")

    taste = service.generate(profile_id)
    taste.user_overrides = {"hard_avoids": ["musicals"]}
    db_session.flush()

    service.generate(profile_id)  # regenerate
    db_session.refresh(taste)
    assert taste.user_overrides == {"hard_avoids": ["musicals"]}


def test_effective_profile_overrides_win(db_session: Session) -> None:
    taste = TasteProfile(
        profile_id=uuid.uuid4(),
        structured={"hard_avoids": ["gore"], "summary": "x"},
        user_overrides={"hard_avoids": ["musicals"]},
    )
    merged = effective_profile(taste)
    assert merged["hard_avoids"] == ["musicals"]
    assert merged["summary"] == "x"


def _client(session: Session, *, with_llm: bool = False) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    if with_llm:
        app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(completion=CANNED)
    return TestClient(app)


def test_taste_api_404_before_generation(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    assert _client(db_session).get(f"/profiles/{profile_id}/taste").status_code == 404


def test_taste_api_generate_then_get_then_edit(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    client = _client(db_session, with_llm=True)

    generated = client.post(f"/profiles/{profile_id}/taste/generate")
    assert generated.status_code == 200
    assert generated.json()["confidence"] == 0.7

    fetched = client.get(f"/profiles/{profile_id}/taste").json()
    assert fetched["structured"]["affinities"]["Science Fiction"] == 0.9

    edited = client.put(
        f"/profiles/{profile_id}/taste",
        json={"userOverrides": {"hard_avoids": ["musicals"]}},
    )
    assert edited.status_code == 200
    assert edited.json()["structured"]["hard_avoids"] == ["musicals"]


def test_generate_without_llm_key_400(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    # No get_llm_provider override and no LLM_API_KEY configured -> 400.
    assert _client(db_session).post(f"/profiles/{profile_id}/taste/generate").status_code == 400


# --- automatic taste refresh on ingest --------------------------------------


def _stored_taste(session: Session, profile_id: uuid.UUID) -> TasteProfile | None:
    return session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))


def test_maybe_refresh_taste_generates_with_llm(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    assert maybe_refresh_taste(db_session, profile_id, FakeLLMProvider(completion=CANNED)) is True
    taste = _stored_taste(db_session, profile_id)
    assert taste is not None and taste.summary_text is not None


def test_maybe_refresh_taste_noops_offline(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    # No LLM configured -> no taste written, deterministic engine still personalises elsewhere.
    assert maybe_refresh_taste(db_session, profile_id, None) is False
    assert _stored_taste(db_session, profile_id) is None


def test_maybe_refresh_taste_swallows_llm_failure(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)

    class _Boom:
        def complete(self, prompt: str) -> str:
            raise RuntimeError("llm down")

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("llm down")

    # A taste failure must never break ingestion.
    assert maybe_refresh_taste(db_session, profile_id, _Boom()) is False
    assert _stored_taste(db_session, profile_id) is None


def test_optional_llm_provider_none_without_key() -> None:
    assert optional_llm_provider() is None


def test_sample_data_auto_generates_taste(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user never asks for taste — loading data generates it automatically.
    monkeypatch.setattr(
        "phare.api.profiles.optional_llm_provider",
        lambda: FakeLLMProvider(completion=CANNED),
    )
    profile = Profile(display_name="auto")
    db_session.add(profile)
    db_session.flush()
    client = _client(db_session)

    assert client.post(f"/profiles/{profile.id}/sample-data").status_code == 200

    fetched = client.get(f"/profiles/{profile.id}/taste")
    assert fetched.status_code == 200
    assert fetched.json()["summary"] is not None
