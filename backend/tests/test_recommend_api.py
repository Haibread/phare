"""HTTP journey for recommendations + chat, wired with the offline embedder (no LLM key)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.deps import (
    Embedder,
    get_embedder,
    get_optional_agent_llm,
    get_optional_chat_llm,
)
from phare.db.models import User
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider
from tests.conftest import authed_client, make_account


def _offline_overrides() -> dict:
    return {
        get_embedder: lambda: Embedder(
            provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
        ),
        get_optional_chat_llm: lambda: None,
    }


def _client(session: Session, user: User) -> TestClient:
    return authed_client(session, user, overrides=_offline_overrides())


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


def _profile_with_data(client: TestClient, user: User) -> str:
    profile_id = str(user.profile.id)
    client.post(f"/profiles/{profile_id}/sample-data")
    client.post("/catalog/sample")
    return profile_id


def test_full_recommendations_journey(db_session: Session) -> None:
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = _profile_with_data(client, user)

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


def test_recommendations_localise_row_titles_via_accept_language(db_session: Session) -> None:
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = _profile_with_data(client, user)

    english = client.get(f"/profiles/{profile_id}/recommendations").json()["rows"]
    french = client.get(
        f"/profiles/{profile_id}/recommendations", headers={"Accept-Language": "fr"}
    ).json()["rows"]

    en_titles = {row["key"]: row["title"] for row in english}
    fr_titles = {row["key"]: row["title"] for row in french}
    assert en_titles["you_might_like"] == "You might like"
    assert fr_titles["you_might_like"] == "Pourrait vous plaire"
    # Offline path: explanations are templated, so they localise too.
    fr_items = next(row["items"] for row in french if row["key"] == "you_might_like")
    assert all(item["explanation"] for item in fr_items)


def test_recommendations_404_for_unknown_profile(db_session: Session) -> None:
    client = _client(db_session, make_account(db_session))
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/profiles/{missing}/recommendations").status_code == 404


def test_title_detail_returns_synopsis_and_links(db_session: Session) -> None:
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = _profile_with_data(client, user)
    items = client.get(f"/profiles/{profile_id}/recommendations").json()["rows"][0]["items"]
    title_id = items[0]["titleId"]

    detail = client.get(f"/titles/{title_id}").json()
    assert detail["titleId"] == title_id
    assert detail["title"]
    assert "overview" in detail  # the synopsis field is present (may be null for thin records)
    assert "genres" in detail and "runtimeMinutes" in detail

    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/titles/{missing}").status_code == 404


def test_title_explanation_is_lazy_and_templates_offline(db_session: Session) -> None:
    user = make_account(db_session)
    client = _client(db_session, user)  # offline: no workhorse LLM
    profile_id = _profile_with_data(client, user)
    title_id = client.get(f"/profiles/{profile_id}/recommendations").json()["rows"][0]["items"][0][
        "titleId"
    ]

    resp = client.get(f"/profiles/{profile_id}/titles/{title_id}/explanation")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(resp.text)
    reply = "".join(e["data"]["text"] for e in events if e["event"] == "delta")
    assert reply and events[-1]["event"] == "done"  # deterministic template streamed, no LLM call

    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/profiles/{profile_id}/titles/{missing}/explanation").status_code == 404


def test_title_explanation_streams_the_workhorse_when_available(db_session: Session) -> None:
    user = make_account(db_session)
    overrides = {
        get_embedder: lambda: Embedder(
            provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
        ),
        get_optional_chat_llm: lambda: FakeLLMProvider(
            completion="Because its cerebral mood is squarely your taste."
        ),
    }
    client = authed_client(db_session, user, overrides=overrides)
    profile_id = _profile_with_data(client, user)
    title_id = client.get(f"/profiles/{profile_id}/recommendations").json()["rows"][0]["items"][0][
        "titleId"
    ]

    events = _sse_events(client.get(f"/profiles/{profile_id}/titles/{title_id}/explanation").text)
    deltas = [e for e in events if e["event"] == "delta"]
    assert len(deltas) > 1  # streamed in chunks (the fake yields word-by-word), not one blob
    assert "".join(d["data"]["text"] for d in deltas) == (
        "Because its cerebral mood is squarely your taste."
    )


def test_title_explanation_anchors_on_a_because_you_watched_seed(db_session: Session) -> None:
    import uuid

    from sqlalchemy import select

    from phare.db.models import Title, WatchEvent

    user = make_account(db_session)
    # One shared fake so we can inspect the exact prompt the endpoint built.
    fake = FakeLLMProvider(completion="Because it shares that film's epic scope, it's your kind.")
    overrides = {
        get_embedder: lambda: Embedder(
            provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
        ),
        get_optional_chat_llm: lambda: fake,
    }
    client = authed_client(db_session, user, overrides=overrides)
    profile_id = _profile_with_data(client, user)

    # A title the viewer actually watched — the only kind of anchor the endpoint will honour.
    seed = db_session.execute(
        select(Title)
        .join(WatchEvent, WatchEvent.title_id == Title.id)
        .where(WatchEvent.profile_id == user.profile.id)
        .limit(1)
    ).scalar_one()
    rec_title_id = client.get(f"/profiles/{profile_id}/recommendations").json()["rows"][0]["items"][
        0
    ]["titleId"]

    client.get(f"/profiles/{profile_id}/titles/{rec_title_id}/explanation?because={seed.id}")
    anchored = fake.prompts[-1]
    assert "watched and loved" in anchored  # the reason opens from the seed...
    assert seed.title in anchored  # ...and names it

    # An anchor the viewer never watched is ignored — falls back to the taste-only prompt.
    fake.prompts.clear()
    client.get(f"/profiles/{profile_id}/titles/{rec_title_id}/explanation?because={uuid.uuid4()}")
    assert "watched and loved" not in fake.prompts[-1]


def test_chat_journey(db_session: Session) -> None:
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = _profile_with_data(client, user)

    response = client.post(
        f"/profiles/{profile_id}/chat", json={"message": "something funny and short"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"]["includeGenres"] == ["Comedy"]
    assert body["intent"]["maxRuntime"] == 100  # "short" -> 100-minute ceiling
    assert "replyText" in body


def test_chat_stream_offline_emits_meta_then_reply(db_session: Session) -> None:
    user = make_account(db_session)
    # offline (no LLM): deterministic reply, still streamed as SSE
    client = _client(db_session, user)
    profile_id = _profile_with_data(client, user)

    response = client.post(
        f"/profiles/{profile_id}/chat/stream", json={"message": "something funny and short"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _sse_events(response.text)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "status"  # instant acknowledgement, before any model call
    assert kinds[-1] == "done"
    meta = next(e for e in events if e["event"] == "meta")
    assert meta["data"]["intent"]["includeGenres"] == ["Comedy"]
    reply = "".join(e["data"]["text"] for e in events if e["event"] == "delta")
    assert reply  # a (deterministic) reply was streamed


def test_chat_stream_with_llm_streams_chunks(db_session: Session) -> None:
    user = make_account(db_session)
    overrides = {
        get_embedder: lambda: Embedder(
            provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
        ),
        # Workhorse plans a recommend; the agent model streams the natural reply word-by-word.
        get_optional_chat_llm: lambda: FakeLLMProvider(
            completion='{"calls":[{"tool":"recommend","args":{}}]}'
        ),
        get_optional_agent_llm: lambda: FakeLLMProvider(
            completion="A few ideas you might enjoy tonight!"
        ),
    }
    client = authed_client(db_session, user, overrides=overrides)
    profile_id = _profile_with_data(client, user)

    response = client.post(
        f"/profiles/{profile_id}/chat/stream", json={"message": "something to watch"}
    )
    assert response.status_code == 200
    events = _sse_events(response.text)

    deltas = [e for e in events if e["event"] == "delta"]
    assert len(deltas) > 1  # streamed in multiple chunks, not one blob
    assert "".join(d["data"]["text"] for d in deltas) == "A few ideas you might enjoy tonight!"
    assert events[0]["event"] == "status"  # progress surfaced before the picks
    meta = next(e for e in events if e["event"] == "meta")
    assert meta["data"]["items"]  # picks surfaced in the meta event


def test_catalog_sample_idempotent_over_http(db_session: Session) -> None:
    client = _client(db_session, make_account(db_session))
    first = client.post("/catalog/sample").json()["created"]
    second = client.post("/catalog/sample").json()["created"]
    assert first > 0
    assert second == 0


def test_recommendations_isolated_per_profile(db_session: Session) -> None:
    # Two distinct accounts; each user can only reach its own profile.
    user_a = make_account(db_session, display_name="me", email="me@example.test")
    user_b = make_account(db_session, display_name="other", email="other@example.test")
    client_a = _client(db_session, user_a)
    client_b = _client(db_session, user_b)
    a = _profile_with_data(client_a, user_a)
    b = str(user_b.profile.id)

    # B has no history: no you_might_like, and nothing of A's leaks in.
    b_rows = {row["key"] for row in client_b.get(f"/profiles/{b}/recommendations").json()["rows"]}
    assert "you_might_like" not in b_rows
    a_rows = {row["key"] for row in client_a.get(f"/profiles/{a}/recommendations").json()["rows"]}
    assert "you_might_like" in a_rows
    # Cross-user access is a 404, not a leak.
    assert client_b.get(f"/profiles/{a}/recommendations").status_code == 404
