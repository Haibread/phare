"""Chat agent: keyword intent parsing + the engine honouring intent filters."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from phare.agent.intent import keyword_intent
from phare.agent.schema import AgentAction, ChatIntent
from phare.agent.service import (
    ChatService,
    _compose_reply_template,
    _reply_text,
    _strip_leading_think,
    build_compose_prompt,
)
from phare.agent.tools import ExecutionResult
from phare.catalog.sample import seed_sample_catalog
from phare.db.models import Profile
from phare.ingest.sample import seed_sample_data
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider
from phare.recommend.service import RecommendationService
from phare.recommend.taste_vector import watched_title_ids


def test_composer_prompt_carries_french_directive() -> None:
    result = ExecutionResult()
    assert "French" in build_compose_prompt("salut", result, "fr")
    # English: no output-language directive is appended.
    assert "Write your response in" not in build_compose_prompt("hi", result)


def test_offline_reply_text_localises() -> None:
    intent = ChatIntent(include_genres=["Comedy"], max_runtime=90)
    assert _reply_text(intent, 0, "fr").startswith("Je n'ai rien trouvé")
    fr = _reply_text(intent, 3, "fr")
    assert fr.startswith("Voici quelques suggestions") and "90 minutes" in fr


def test_template_reply_localises_framing() -> None:
    result = ExecutionResult(actions=[AgentAction(kind="logged_signal", summary="loved Heat")])
    fr = _compose_reply_template(result, "fr")
    assert fr.startswith("C'est noté — loved Heat")  # framing localised, tool summary verbatim


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


def test_strip_leading_think_drops_reasoning_block() -> None:
    chunks = ["<think>let me ", "consider</think>Try ", "Prisoners next."]
    assert "".join(_strip_leading_think(iter(chunks))) == "Try Prisoners next."


def test_strip_leading_think_passes_through_a_normal_reply() -> None:
    chunks = ["So glad ", "you loved it!"]
    assert "".join(_strip_leading_think(iter(chunks))) == "So glad you loved it!"


def test_strip_leading_think_yields_nothing_for_reasoning_only() -> None:
    # All reasoning, no answer after — yields nothing, so the caller falls back to the template.
    assert "".join(_strip_leading_think(iter(["<think>only ", "thinking</think>"]))) == ""


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


def test_keyword_intent_detects_rewatch() -> None:
    assert keyword_intent("a comfort rewatch").rewatch is True
    assert keyword_intent("something I want to watch again").rewatch is True
    assert keyword_intent("revisit an old favorite").rewatch is True
    assert keyword_intent("I've seen it before but rewatch it").rewatch is True
    assert keyword_intent("something funny and new").rewatch is False


def test_empty_result_replies_deterministically_without_the_agent_model(
    db_session: Session,
) -> None:
    # History but no catalog → recommend has nothing new to return. The agent model must NOT be
    # invoked on an empty grounded list (it would hallucinate titles from memory); the turn falls
    # back to an honest deterministic reply instead.
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()
    seed_sample_data(db_session, profile.id)
    db_session.flush()

    planner = FakeLLMProvider(completion='{"calls": [{"tool": "recommend", "args": {}}]}')
    service = ChatService(_recommender(db_session), chat_llm=planner)
    prepared = service.prepare(profile.id, "something new and weird")

    assert prepared.items == []
    assert prepared.compose_prompt is None  # the agent model is not spent on an empty list
    assert prepared.reply_text  # a deterministic "no match" reply instead


def test_chat_flags_degraded_when_planner_output_unparseable(db_session: Session) -> None:
    # A configured model that returns unparseable planner output must surface degraded=True so the
    # UI can flag reduced mode — not silently pretend the agent understood.
    profile_id = _seeded_profile(db_session)

    junk = FakeLLMProvider(completion="not json at all")
    degraded_reply = ChatService(_recommender(db_session), chat_llm=junk).respond(
        profile_id, "funny"
    )
    assert degraded_reply.degraded is True

    good = FakeLLMProvider(completion='{"calls":[{"tool":"recommend","args":{}}]}')
    ok_reply = ChatService(_recommender(db_session), chat_llm=good).respond(profile_id, "funny")
    assert ok_reply.degraded is False


def test_chat_drops_titles_named_in_the_message(db_session: Session) -> None:
    # Telling the agent about a title must not get it recommended straight back ("I loved X" → X in
    # the strip reads as not listening).
    profile_id = _seeded_profile(db_session)
    service = ChatService(_recommender(db_session), chat_llm=None)

    baseline = service.respond(profile_id, "something good to watch")
    target = next((i.title for i in baseline.items if len(i.title) >= 4), None)
    assert target is not None  # baseline returns nameable picks

    reply = service.respond(profile_id, f"I just watched {target} last night and loved it")
    assert all(item.title != target for item in reply.items)


def test_rewatch_draws_from_watched_history_not_the_fresh_catalog(db_session: Session) -> None:
    profile_id = _seeded_profile(db_session)
    service = ChatService(_recommender(db_session), chat_llm=None)
    watched = watched_title_ids(db_session, profile_id)

    # A normal request suggests only titles the profile has NOT seen.
    fresh = service.respond(profile_id, "something good to watch")
    assert fresh.items
    assert all(item.title_id not in watched for item in fresh.items)

    # A rewatch request flips the source: every pick is something they've already watched.
    again = service.respond(profile_id, "a comfort rewatch")
    assert again.intent.rewatch is True
    assert again.items
    assert all(item.title_id in watched for item in again.items)
