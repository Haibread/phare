"""HTTP journey for recommendations + chat, wired with the offline embedder (no LLM key)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.api.deps import (
    Embedder,
    get_embedder,
    get_optional_agent_llm,
    get_optional_chat_llm,
)
from phare.db.base import get_session
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider


def _client(session: Session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_embedder] = lambda: Embedder(
        provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
    )
    app.dependency_overrides[get_optional_chat_llm] = lambda: None
    return TestClient(app)


def _sse_events(text: str) -> list[dict]:
    """Parse an SSE body into a list of {event, data} dicts."""
    events: list[dict] = []
    for block in text.strip().split("\n\n"):
        event: dict = {}
        for line in block.splitlines():
            if line.startswith("event:"):
                event["event"] = line[len("event:") :].strip()
            elif line.startswith("data:"):
                event["data"] = json.loads(line[len("data:") :].strip())
        if event:
            events.append(event)
    return events


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


def test_title_detail_returns_synopsis_and_links(db_session: Session) -> None:
    client = _client(db_session)
    profile_id = _profile_with_data(client)
    items = client.get(f"/profiles/{profile_id}/recommendations").json()["rows"][0]["items"]
    title_id = items[0]["titleId"]

    detail = client.get(f"/titles/{title_id}").json()
    assert detail["titleId"] == title_id
    assert detail["title"]
    assert "overview" in detail  # the synopsis field is present (may be null for thin records)
    assert "genres" in detail and "runtimeMinutes" in detail

    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/titles/{missing}").status_code == 404


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


def test_chat_stream_offline_emits_meta_then_reply(db_session: Session) -> None:
    client = _client(db_session)  # offline (no LLM): deterministic reply, still streamed as SSE
    profile_id = _profile_with_data(client)

    response = client.post(
        f"/profiles/{profile_id}/chat/stream", json={"message": "something funny and short"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _sse_events(response.text)
    assert events[0]["event"] == "meta"
    assert events[-1]["event"] == "done"
    assert events[0]["data"]["intent"]["includeGenres"] == ["Comedy"]
    reply = "".join(e["data"]["text"] for e in events if e["event"] == "delta")
    assert reply  # a (deterministic) reply was streamed


def test_chat_stream_with_llm_streams_chunks(db_session: Session) -> None:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_embedder] = lambda: Embedder(
        provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
    )
    # Workhorse plans a recommend; the agent model streams the natural reply word-by-word.
    app.dependency_overrides[get_optional_chat_llm] = lambda: FakeLLMProvider(
        completion='{"calls":[{"tool":"recommend","args":{}}]}'
    )
    app.dependency_overrides[get_optional_agent_llm] = lambda: FakeLLMProvider(
        completion="A few ideas you might enjoy tonight!"
    )
    client = TestClient(app)
    profile_id = _profile_with_data(client)

    response = client.post(
        f"/profiles/{profile_id}/chat/stream", json={"message": "something to watch"}
    )
    assert response.status_code == 200
    events = _sse_events(response.text)

    deltas = [e for e in events if e["event"] == "delta"]
    assert len(deltas) > 1  # streamed in multiple chunks, not one blob
    assert "".join(d["data"]["text"] for d in deltas) == "A few ideas you might enjoy tonight!"
    assert events[0]["data"]["items"]  # picks surfaced in the meta event


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
