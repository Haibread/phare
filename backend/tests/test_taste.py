"""Taste extraction service + API (generate, view, edit, override persistence)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.taste import get_llm_provider
from phare.core.config import get_settings
from phare.db.models import EventType, Profile, TasteProfile, Title, User, WatchEvent
from phare.ingest.sample import seed_sample_data
from phare.providers.fakes import FakeLLMProvider
from phare.taste.service import (
    TasteService,
    effective_profile,
    maybe_refresh_taste,
    optional_llm_provider,
)
from tests.conftest import authed_client, make_account

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


def _account_with_history(session: Session) -> User:
    user = make_account(session)
    seed_sample_data(session, user.profile.id)
    session.flush()
    return user


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


def test_generate_localises_only_the_summary(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=CANNED)

    TasteService(db_session, llm, "test-model", language="fr").generate(profile_id)

    prompt = llm.prompts[0]
    assert "summary` field in French" in prompt  # the human-readable summary localises
    assert "in English" in prompt  # structured keys stay English so affinity matching holds


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


def _client(session: Session, user: User, *, with_llm: bool = False) -> TestClient:
    overrides = {get_llm_provider: lambda: FakeLLMProvider(completion=CANNED)} if with_llm else None
    return authed_client(session, user, overrides=overrides)


def test_taste_api_404_before_generation(db_session: Session) -> None:
    user = _account_with_history(db_session)
    assert _client(db_session, user).get(f"/profiles/{user.profile.id}/taste").status_code == 404


def test_taste_api_generate_then_get_then_edit(db_session: Session) -> None:
    user = _account_with_history(db_session)
    profile_id = user.profile.id
    client = _client(db_session, user, with_llm=True)

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


def test_generate_is_rate_limited_after_a_recent_generation(db_session: Session) -> None:
    user = _account_with_history(db_session)
    profile_id = user.profile.id
    client = _client(db_session, user, with_llm=True)

    first = client.post(f"/profiles/{profile_id}/taste/generate")
    assert first.status_code == 200

    # A second click right away must not spend another LLM call — it's on cooldown.
    second = client.post(f"/profiles/{profile_id}/taste/generate")
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert int(second.headers["Retry-After"]) > 0


def test_generate_without_llm_key_400(db_session: Session) -> None:
    user = _account_with_history(db_session)
    # No get_llm_provider override and no LLM_API_KEY configured -> 400.
    assert (
        _client(db_session, user).post(f"/profiles/{user.profile.id}/taste/generate").status_code
        == 400
    )


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
        def complete(self, prompt: str, *, max_tokens: int | None = None) -> str:
            raise RuntimeError("llm down")

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("llm down")

    # A taste failure must never break ingestion.
    assert maybe_refresh_taste(db_session, profile_id, _Boom()) is False
    assert _stored_taste(db_session, profile_id) is None


def _add_events(session: Session, profile_id: uuid.UUID, count: int) -> None:
    """Append ``count`` fresh watch events so the auto-refresh gate sees drift.

    ``ingested_at`` is stamped explicitly because Postgres freezes ``now()`` at transaction start,
    and the whole test runs in one transaction — so the server default would land *before* the
    taste's ``generated_at`` and the gate wouldn't see these as new.
    """
    title = session.scalar(select(Title))
    assert title is not None
    now = datetime.now(UTC)
    for i in range(count):
        session.add(
            WatchEvent(
                profile_id=profile_id,
                title_id=title.id,
                type=EventType.watched,
                source="test",
                external_ref=f"gate-{uuid.uuid4()}-{i}",
                ingested_at=now,
            )
        )
    session.flush()


def test_auto_refresh_skips_when_nothing_changed(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=CANNED)
    # First call generates (no profile yet); the immediate second call has zero new events → skip.
    assert maybe_refresh_taste(db_session, profile_id, llm) is True
    generated_at = _stored_taste(db_session, profile_id).generated_at
    assert maybe_refresh_taste(db_session, profile_id, llm) is False
    assert len(llm.prompts) == 1  # the gate spared a second full extraction
    assert _stored_taste(db_session, profile_id).generated_at == generated_at


def test_auto_refresh_runs_after_enough_new_events(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=CANNED)
    assert maybe_refresh_taste(db_session, profile_id, llm) is True
    _add_events(db_session, profile_id, get_settings().taste_refresh_min_events)
    # Enough drift since the last generation → worth a re-read.
    assert maybe_refresh_taste(db_session, profile_id, llm) is True
    assert len(llm.prompts) == 2


def test_auto_refresh_folds_in_trickle_once_stale(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=CANNED)
    assert maybe_refresh_taste(db_session, profile_id, llm) is True
    # A single new event isn't enough on its own...
    _add_events(db_session, profile_id, 1)
    assert maybe_refresh_taste(db_session, profile_id, llm) is False
    # ...but once the profile is older than the interval, the trickle gets folded in.
    taste = _stored_taste(db_session, profile_id)
    interval = get_settings().taste_refresh_min_interval_seconds
    taste.generated_at = datetime.now(UTC) - timedelta(seconds=interval + 60)
    db_session.flush()
    assert maybe_refresh_taste(db_session, profile_id, llm) is True


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
    user = make_account(db_session, display_name="auto")
    profile_id = user.profile.id
    client = _client(db_session, user)

    assert client.post(f"/profiles/{profile_id}/sample-data").status_code == 200

    fetched = client.get(f"/profiles/{profile_id}/taste")
    assert fetched.status_code == 200
    assert fetched.json()["summary"] is not None
