"""HTTP journey for recommendations + chat, wired with the offline embedder (no LLM key)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.api.deps import Embedder, get_embedder, get_optional_chat_llm
from phare.db.base import get_session
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider


def _client(session: Session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_embedder] = lambda: Embedder(
        provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
    )
    app.dependency_overrides[get_optional_chat_llm] = lambda: None
    return TestClient(app)


def _profile_with_data(client: TestClient) -> str:
    profile_id = client.post("/profiles", json={"displayName": "me"}).json()["id"]
    client.post(f"/profiles/{profile_id}/sample-data")
    client.post("/catalog/sample")
    return profile_id


def test_full_recommendations_journey(db_session: Session) -> None:
    client = _client(db_session)
    profile_id = _profile_with_data(client)

    response = client.get(f"/profiles/{profile_id}/recommendations")
    assert response.status_code == 200
    rows = {row["key"]: row for row in response.json()["rows"]}

    assert "you_might_like" in rows
    items = rows["you_might_like"]["items"]
    assert items
    assert all(item["explanation"] for item in items)
    assert all("score" in item["components"] for item in items)  # transparent breakdown on the wire
    # Camel-case serialisation holds across the new DTOs.
    assert "isSwing" in items[0]


def test_recommendations_404_for_unknown_profile(db_session: Session) -> None:
    client = _client(db_session)
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/profiles/{missing}/recommendations").status_code == 404


def test_chat_journey(db_session: Session) -> None:
    client = _client(db_session)
    profile_id = _profile_with_data(client)

    response = client.post(
        f"/profiles/{profile_id}/chat", json={"message": "something funny and short"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"]["includeGenres"] == ["Comedy"]
    assert body["intent"]["maxRuntime"] == 100  # "short" -> 100-minute ceiling
    assert "replyText" in body


def test_catalog_sample_idempotent_over_http(db_session: Session) -> None:
    client = _client(db_session)
    first = client.post("/catalog/sample").json()["created"]
    second = client.post("/catalog/sample").json()["created"]
    assert first > 0
    assert second == 0


def test_recommendations_isolated_per_profile(db_session: Session) -> None:
    client = _client(db_session)
    a = _profile_with_data(client)
    b = client.post("/profiles", json={"displayName": "other"}).json()["id"]
    # B has no history: no you_might_like, and nothing of A's leaks in.
    b_rows = {row["key"] for row in client.get(f"/profiles/{b}/recommendations").json()["rows"]}
    assert "you_might_like" not in b_rows
    a_rows = {row["key"] for row in client.get(f"/profiles/{a}/recommendations").json()["rows"]}
    assert "you_might_like" in a_rows
