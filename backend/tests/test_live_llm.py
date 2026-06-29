"""Live smoke tests against the configured LLM provider.

These make real network calls (and cost a few fractions of a cent), so they are **opt-in**: they
only run when ``PHARE_LIVE_LLM=1`` is set, and skip cleanly without an ``LLM_API_KEY``. They never
run in CI or on a normal ``pytest`` invocation. Use them to verify a real provider config end to
end — that the chat/agent models answer and the embedding model returns vectors that fit the schema.

    PHARE_LIVE_LLM=1 uv run pytest tests/test_live_llm.py -q
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.orm import Session

from phare.core.config import get_settings
from phare.embeddings.version import get_embedding_provider
from phare.providers.llm import OpenAILLMProvider
from tests.conftest import authed_client, make_account

pytestmark = pytest.mark.skipif(
    not os.environ.get("PHARE_LIVE_LLM"),
    reason="set PHARE_LIVE_LLM=1 to run live provider smoke tests",
)


def _settings():
    settings = get_settings()
    if not settings.llm_api_key:
        pytest.skip("no LLM_API_KEY configured")
    return settings


def _chat_provider(model: str) -> OpenAILLMProvider:
    settings = get_settings()
    return OpenAILLMProvider(
        api_key=settings.llm_api_key or "",
        chat_model=model,
        embedding_model=settings.llm_embedding_model,
        base_url=settings.llm_base_url,
    )


def test_chat_model_responds() -> None:
    settings = _settings()
    reply = _chat_provider(settings.llm_chat_model).complete("Reply with the single word: pong")
    assert reply.strip()  # the workhorse model is reachable and answers


def test_agent_model_responds() -> None:
    settings = _settings()
    reply = _chat_provider(settings.agent_chat_model).complete("Reply with the single word: pong")
    assert reply.strip()  # the (possibly bigger) agent model is reachable on this endpoint


def _turn(
    client, profile_id: str, message: str, history: list[dict], active_intent: dict | None = None
) -> dict:
    body: dict = {"message": message, "history": history}
    if active_intent is not None:
        body["activeIntent"] = active_intent
    response = client.post(f"/profiles/{profile_id}/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_live_follow_up_resolves_against_conversation_history(db_session: Session) -> None:
    """A real two-turn chat: 'something funny and short' then 'even shorter'. With the first turn
    replayed as history, the model should treat the follow-up as a *shorter comedy* rather than a
    blind request. Run with -s to read the transcript:

        PHARE_LIVE_LLM=1 uv run pytest tests/test_live_llm.py -k follow_up -s
    """
    _settings()  # skip if no key configured
    # No LLM dependency overrides → the app builds the real providers from .env (PHARE_LIVE_LLM
    # keeps the creds). Seed sample data (taste) + the offline sample catalog, embedded live.
    user = make_account(db_session)
    client = authed_client(db_session, user)
    profile_id = str(user.profile.id)
    client.post(f"/profiles/{profile_id}/sample-data")
    client.post("/catalog/sample")

    first = _turn(client, profile_id, "something funny and short", [])
    history = [
        {"role": "user", "text": "something funny and short"},
        {"role": "agent", "text": first["replyText"]},
    ]
    # Warm: replay the transcript AND the filters in effect, so "even shorter" can tighten the cap.
    warm = _turn(client, profile_id, "even shorter", history, active_intent=first["intent"])
    cold = _turn(client, profile_id, "even shorter", [])  # same follow-up, no context at all

    print("\n--- turn 1: 'something funny and short' ---")
    print("reply:", first["replyText"])
    print("intent:", first["intent"])
    print("picks:", [i["title"] for i in first["items"][:5]])
    print("\n--- turn 2 WITH history: 'even shorter' ---")
    print("reply:", warm["replyText"])
    print("intent:", warm["intent"])
    print("picks:", [i["title"] for i in warm["items"][:5]])
    print("\n--- turn 2 COLD (no history): 'even shorter' ---")
    print("reply:", cold["replyText"])
    print("intent:", cold["intent"])

    assert first["replyText"] and warm["replyText"]  # both turns produced a real reply


def test_live_clarify_fires_on_vague_and_not_on_specific(db_session: Session) -> None:
    """The high-bar clarify move: a maximally-vague ask from a profile with no taste should get a
    question with quick-replies; a specific ask should just recommend. Run with -s to read both:

        PHARE_LIVE_LLM=1 uv run pytest tests/test_live_llm.py -k clarify -s
    """
    _settings()
    # Fresh profile: catalog to recommend from, but NO sample-data, so there's no taste to lean on.
    user = make_account(db_session)
    client = authed_client(db_session, user)
    profile_id = str(user.profile.id)
    client.post("/catalog/sample")

    vague = _turn(client, profile_id, "what should I watch?", [])
    specific = _turn(client, profile_id, "a tense sci-fi thriller, under two hours", [])

    print("\n--- vague: 'what should I watch?' ---")
    print("reply:", vague["replyText"])
    print("suggestions:", vague["suggestions"])
    print("picks:", [i["title"] for i in vague["items"][:5]])
    print("\n--- specific: 'a tense sci-fi thriller, under two hours' ---")
    print("reply:", specific["replyText"])
    print("suggestions:", specific["suggestions"])
    print("picks:", [i["title"] for i in specific["items"][:5]])

    # The escape hatch is always present when it asks; a specific ask shouldn't stall on a question.
    if vague["suggestions"]:
        assert any("surprise" in s.lower() for s in vague["suggestions"])
    assert not specific["suggestions"]  # specific request recommends, never clarifies


def test_live_clarify_can_ask_again_when_the_user_is_still_vague(db_session: Session) -> None:
    """A user who genuinely doesn't know shouldn't be forced into a blind guess after one question.
    Stay vague across two turns and see whether the agent narrows again (it may) — then bail with
    "surprise me" and confirm it stops asking and recommends. Run with -s to read the flow."""
    _settings()
    user = make_account(db_session)
    client = authed_client(db_session, user)
    profile_id = str(user.profile.id)
    client.post("/catalog/sample")

    t1 = _turn(client, profile_id, "what should I watch?", [])
    history = [
        {"role": "user", "text": "what should I watch?"},
        {"role": "agent", "text": t1["replyText"]},
    ]
    # Still genuinely undecided — not naming a genre, but not bailing either.
    t2 = _turn(client, profile_id, "honestly no idea, I'm just bored", history)
    history += [
        {"role": "user", "text": "honestly no idea, I'm just bored"},
        {"role": "agent", "text": t2["replyText"]},
    ]
    # Now take the out — it must stop asking and recommend.
    t3 = _turn(client, profile_id, "ugh just surprise me", history)

    for label, turn in (("t1 vague", t1), ("t2 still vague", t2), ("t3 'surprise me'", t3)):
        print(f"\n--- {label} ---")
        print("reply:", turn["replyText"])
        print("suggestions:", turn["suggestions"])
        print("picks:", [i["title"] for i in turn["items"][:5]])

    assert not t3["suggestions"]  # "surprise me" is a hard stop — recommend, don't keep asking


def test_embedding_returns_configured_dimension() -> None:
    settings = _settings()
    provider = get_embedding_provider(settings)
    [vector] = provider.embed(["a quiet sci-fi about memory and loss"])
    # With LLM_EMBEDDING_REQUEST_DIMENSIONS=true the model must return schema-sized vectors so they
    # fit Vector(EMBEDDING_DIM) without a migration.
    assert len(vector) == settings.llm_embedding_dim
    assert any(abs(x) > 0 for x in vector)  # a real embedding, not all-zeros
