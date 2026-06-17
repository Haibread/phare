"""Chat agent: keyword intent parsing + the engine honouring intent filters."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from phare.agent.intent import keyword_intent, parse_intent
from phare.agent.service import ChatService
from phare.catalog.sample import seed_sample_catalog
from phare.db.models import Profile
from phare.ingest.sample import seed_sample_data
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider
from phare.recommend.service import RecommendationService


def test_keyword_intent_extracts_runtime_and_genre() -> None:
    intent = keyword_intent("something funny under 90 minutes")
    assert intent.max_runtime == 90
    assert intent.include_genres == ["Comedy"]


def test_keyword_intent_handles_hours_and_short() -> None:
    assert keyword_intent("a 2 hour epic").max_runtime == 120
    assert keyword_intent("something short").max_runtime == 100


def test_keyword_intent_negation_excludes_genre() -> None:
    intent = keyword_intent("a thriller but no horror")
    assert "Thriller" in intent.include_genres
    assert intent.exclude_genres == ["Horror"]


def test_parse_intent_prefers_llm_json() -> None:
    llm = FakeLLMProvider(
        completion='{"max_runtime":120,"include_genres":["Drama"],"exclude_genres":[],"mood":"cozy"}'
    )
    intent = parse_intent("anything cozy", llm)
    assert intent.max_runtime == 120
    assert intent.include_genres == ["Drama"]


def test_parse_intent_falls_back_on_bad_llm_json() -> None:
    intent = parse_intent("funny and short", FakeLLMProvider(completion="not json at all"))
    assert intent.include_genres == ["Comedy"]  # keyword fallback kicked in


def _recommender(session: Session) -> RecommendationService:
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


def test_respond_honours_runtime_filter(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    service = ChatService(_recommender(db_session), chat_llm=None)

    loose = service.respond(profile_id, "anything good")
    assert loose.items  # baseline: the catalog gives recommendations

    # An impossibly tight cap must actually exclude the (all >100-min) catalog candidates.
    tight = service.respond(profile_id, "something under 30 minutes")
    assert tight.intent.max_runtime == 30
    assert tight.items == []
    assert "30 minutes" not in tight.reply_text  # empty -> the graceful message, not a count


def test_respond_empty_history_returns_graceful_message(db_session: Session) -> None:
    profile = Profile(display_name="newbie")
    db_session.add(profile)
    db_session.flush()
    seed_sample_catalog(db_session)

    reply = ChatService(_recommender(db_session), chat_llm=None).respond(profile.id, "funny")
    assert reply.items == []
    assert "couldn't find" in reply.reply_text.lower()
