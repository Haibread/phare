"""Dynamic themed rows: theme selection (offline + LLM) and DB-backed row building."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.api.deps import Embedder, get_embedder, get_optional_chat_llm
from phare.catalog.sample import seed_sample_catalog
from phare.db.base import get_session
from phare.db.models import Profile
from phare.ingest.sample import seed_sample_data
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider
from phare.recommend.dynamic import dynamic_rows, propose_themes
from phare.recommend.service import RecommendationService

_OCTOBER = datetime(2026, 10, 15, tzinfo=UTC)
_MARCH = datetime(2026, 3, 15, tzinfo=UTC)


def test_fallback_uses_season_taste_and_discovery() -> None:
    taste = {"affinities": {"Science Fiction": 0.9, "Musical": -0.5}}
    themes = propose_themes(taste, _OCTOBER, llm=None)
    keys = [t.key for t in themes]

    assert any("Horror" in t.include_genres for t in themes)  # October -> spooky season
    assert any(t.include_genres == ["Science Fiction"] for t in themes)  # top positive affinity
    assert "dyn:something-different" in keys  # discovery row always reserved
    assert len(themes) <= 3


def test_fallback_without_season_still_has_taste_and_discovery() -> None:
    themes = propose_themes({"affinities": {"Drama": 0.8}}, _MARCH, llm=None)
    assert any(t.include_genres == ["Drama"] for t in themes)
    assert any(t.swing_slots >= 3 for t in themes)  # the discovery row


def test_llm_themes_are_parsed() -> None:
    completion = (
        '[{"title":"Cozy mysteries","genres":["Mystery"]},{"title":"Wildcard","genres":[]}]'
    )
    llm = FakeLLMProvider(completion=completion)
    themes = propose_themes({"summary": "x"}, _MARCH, llm=llm)
    assert [t.title for t in themes] == ["Cozy mysteries", "Wildcard"]
    assert themes[0].include_genres == ["Mystery"]


def test_llm_failure_falls_back() -> None:
    themes = propose_themes(
        {"affinities": {"Drama": 0.7}}, _MARCH, llm=FakeLLMProvider(completion="not json")
    )
    assert any(t.include_genres == ["Drama"] for t in themes)


def _service(session: Session) -> RecommendationService:
    return RecommendationService(
        session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=None,
    )


def _seeded(session: Session) -> uuid.UUID:
    profile = Profile(display_name="me")
    session.add(profile)
    session.flush()
    seed_sample_data(session, profile.id)
    seed_sample_catalog(session)
    session.flush()
    return profile.id


def test_dynamic_rows_built_and_genre_scoped(db_session: Session) -> None:
    profile_id = _seeded(db_session)
    rows = dynamic_rows(_service(db_session), profile_id, llm=None, now=_OCTOBER)

    assert rows
    spooky = next((r for r in rows if "Horror" in r.title or r.key.startswith("dyn:spooky")), None)
    assert spooky is not None
    # The spooky row is genre-scoped to horror candidates (catalog has horror titles).
    assert all("Horror" in item.genres for item in spooky.items)


def test_dynamic_endpoint(db_session: Session) -> None:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_embedder] = lambda: Embedder(
        provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
    )
    app.dependency_overrides[get_optional_chat_llm] = lambda: None
    client = TestClient(app)

    profile_id = client.post("/profiles", json={"displayName": "me"}).json()["id"]
    client.post(f"/profiles/{profile_id}/sample-data")
    client.post("/catalog/sample")

    rows = client.get(f"/profiles/{profile_id}/recommendations/dynamic").json()["rows"]
    assert rows
    assert all(row["items"] for row in rows)
    assert all(item["explanation"] for row in rows for item in row["items"])
