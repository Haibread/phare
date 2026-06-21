"""Recommendation logging: rows + chat items are recorded, per-profile, and inspectable."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.api.deps import Embedder, get_embedder, get_optional_chat_llm
from phare.db.models import RecommendationLog, User
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from tests.conftest import authed_client, make_account


def _client(session: Session, user: User) -> TestClient:
    overrides = {
        get_embedder: lambda: Embedder(
            provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
        ),
        get_optional_chat_llm: lambda: None,
    }
    return authed_client(session, user, overrides=overrides)


def _profile_with_data(client: TestClient, user: User) -> str:
    profile_id = str(user.profile.id)
    client.post(f"/profiles/{profile_id}/sample-data")
    client.post("/catalog/sample")
    return profile_id


def test_rows_are_logged_with_metadata(db_session: Session) -> None:
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = _profile_with_data(client, user)

    rows = client.get(f"/profiles/{profile_id}/recommendations").json()["rows"]
    shown = sum(len(row["items"]) for row in rows)

    logged = db_session.scalar(
        select(func.count()).select_from(RecommendationLog).where(RecommendationLog.source == "row")
    )
    assert logged == shown

    # The log is inspectable over HTTP, newest first, with rank + swing preserved.
    log = client.get(f"/profiles/{profile_id}/recommendations/log").json()
    assert log["total"] == shown
    assert {item["source"] for item in log["items"]} == {"row"}
    assert any(item["rowKey"] == "you_might_like" for item in log["items"])


def test_chat_items_are_logged(db_session: Session) -> None:
    user = make_account(db_session)
    client = _client(db_session, user)
    profile_id = _profile_with_data(client, user)

    reply = client.post(f"/profiles/{profile_id}/chat", json={"message": "anything good"}).json()
    chat_logged = db_session.scalar(
        select(func.count())
        .select_from(RecommendationLog)
        .where(RecommendationLog.source == "chat")
    )
    assert chat_logged == len(reply["items"])


def test_log_is_per_profile(db_session: Session) -> None:
    user_a = make_account(db_session, display_name="me", email="me@example.test")
    user_b = make_account(db_session, display_name="b", email="b@example.test")
    client_a = _client(db_session, user_a)
    client_b = _client(db_session, user_b)
    a = _profile_with_data(client_a, user_a)
    b = str(user_b.profile.id)

    client_a.get(f"/profiles/{a}/recommendations")
    b_log = client_b.get(f"/profiles/{b}/recommendations/log").json()
    assert b_log["total"] == 0  # A's recs never appear under B
