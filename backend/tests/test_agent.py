"""Chat agent: keyword intent parsing + the engine honouring intent filters."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from phare.agent import planner
from phare.agent.intent import keyword_intent
from phare.agent.schema import (
    AgentAction,
    ChatIntent,
    ChatMessage,
    format_active_intent,
    format_history,
)
from phare.agent.service import (
    ChatService,
    _clarify_suggestions,
    _compose_reply_template,
    _reply_text,
    _strip_leading_think,
    build_compose_prompt,
    intent_filter,
)
from phare.agent.tools import ExecutionResult
from phare.catalog.sample import seed_sample_catalog
from phare.db.models import Profile
from phare.ingest.sample import seed_sample_data
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider
from phare.recommend.schema import Candidate, Recommendation
from phare.recommend.service import RecommendationService
from phare.recommend.taste_vector import watched_title_ids


def test_composer_prompt_carries_french_directive() -> None:
    result = ExecutionResult()
    assert "French" in build_compose_prompt("salut", result, "fr")
    # English: no output-language directive is appended.
    assert "Write your response in" not in build_compose_prompt("hi", result)


def test_format_history_bounds_count_and_truncates_text() -> None:
    msgs = [ChatMessage(role="user" if i % 2 == 0 else "agent", text=f"msg {i}") for i in range(20)]
    block = format_history(msgs)
    lines = block.splitlines()
    assert len(lines) == 6  # only the last few turns reach the prompt
    assert "msg 19" in lines[-1] and "msg 14" in lines[0]  # the last six (14..19), oldest dropped
    assert "msg 13" not in block

    block = format_history(
        [ChatMessage(role="user", text="x" * 1000), ChatMessage(role="agent", text="   ")]
    )
    assert block.startswith("User: ") and block.endswith("…")  # long text truncated
    assert "\n" not in block  # the blank agent turn is dropped, not rendered as an empty line
    assert format_history([]) == ""  # no history → no dangling header for callers to emit


def test_compose_prompt_includes_conversation_history() -> None:
    result = ExecutionResult()
    history = [
        ChatMessage(role="user", text="something funny"),
        ChatMessage(role="agent", text="Try Paddington 2."),
    ]
    prompt = build_compose_prompt("even shorter", result, "en", history)
    assert "Recent conversation:" in prompt
    assert "something funny" in prompt and "Paddington 2" in prompt
    # No history → no dangling header.
    assert "Recent conversation:" not in build_compose_prompt("hi", result)


def test_format_active_intent_summarises_filters() -> None:
    summary = format_active_intent(
        ChatIntent(
            include_genres=["Comedy"], exclude_genres=["Horror"], max_runtime=40, mood="cozy"
        )
    )
    assert "Comedy" in summary and "no Horror" in summary
    assert "≤40 min" in summary and "cozy" in summary
    assert format_active_intent(ChatIntent()) == ""  # nothing in effect → empty, no dangling line


def test_planner_prompt_carries_active_filters(db_session: Session) -> None:
    # The runtime cap lives only in the structured intent, never the prose — so the planner needs
    # the active-filters line to tighten "even shorter" below the prior cap instead of guessing.
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()

    fake = FakeLLMProvider(completion='{"calls": [{"tool": "recommend", "args": {}}]}')
    active = ChatIntent(include_genres=["Comedy"], max_runtime=40)
    planner.plan(
        db_session, profile.id, "even shorter", fake, now=datetime.now(UTC), active_intent=active
    )
    prompt = fake.prompts[0]
    assert "Active filters" in prompt
    assert "Comedy" in prompt and "≤40 min" in prompt


def test_clarify_suggestions_bounds_and_guarantees_escape_hatch() -> None:
    chips = _clarify_suggestions({"suggestions": ["a", "b", "c", "d", "e"]})
    assert chips == ["a", "b", "c", "Surprise me"]  # capped at 3, escape hatch appended
    # The model already offered an out → don't double it.
    assert _clarify_suggestions({"suggestions": ["a film", "surprise me"]}) == [
        "a film",
        "surprise me",
    ]
    assert _clarify_suggestions({}) == ["Surprise me"]  # nothing usable → just the escape hatch
    assert _clarify_suggestions({"suggestions": []}, "fr")[-1] == "Surprends-moi"  # localised


def test_clarify_asks_one_question_without_spending_the_agent_model(db_session: Session) -> None:
    # A genuinely vague request: the planner asks instead of guessing. The question is the workhorse
    # planner's own text (no agent-model call), and an escape hatch is always present (guardrail 5).
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()

    clarify = (
        '{"calls": [{"tool": "clarify", "args": {"question": "A quick film or a series?", '
        '"suggestions": ["a film", "a series"]}}]}'
    )
    fake = FakeLLMProvider(completion=clarify)
    reply = ChatService(_recommender(db_session), chat_llm=fake).respond(
        profile.id, "recommend me something"
    )
    assert reply.items == []
    assert reply.reply_text == "A quick film or a series?"
    assert reply.suggestions == ["a film", "a series", "Surprise me"]
    assert len(fake.prompts) == 1  # only the planner ran; the agent model was never spent


def test_planner_prompt_offers_clarify_as_a_high_bar_option() -> None:
    from phare.agent.planner import _SYSTEM

    # The move exists, is gated high, and may repeat across turns (no blunt "once only" cap) — the
    # escape hatch, not a count, is what stops a loop.
    assert "clarify" in _SYSTEM and "bar is high" in _SYSTEM
    assert "MAY ask again" in _SYSTEM


def test_planner_prompt_carries_conversation_history(db_session: Session) -> None:
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()

    fake = FakeLLMProvider(completion='{"calls": [{"tool": "recommend", "args": {}}]}')
    history = [
        ChatMessage(role="user", text="something funny"),
        ChatMessage(role="agent", text="Try Paddington 2."),
    ]
    planner.plan(
        db_session, profile.id, "even shorter", fake, now=datetime.now(UTC), history=history
    )
    prompt = fake.prompts[0]
    assert "Recent conversation:" in prompt
    assert "something funny" in prompt and "Paddington 2" in prompt
    assert prompt.index("Recent conversation:") < prompt.index("User message: even shorter")


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


def test_composer_prompt_titles_follow_the_displayed_order() -> None:
    # B1: the composer and the strip are the same ordered list now (score order, since M1.1), so the
    # reply leads with what's actually on top of the strip — no more "why these?" naming positions
    # 4-6. Lock that the prompt lists the leading items in payload order.
    items = [
        Recommendation(
            title_id=uuid.uuid4(), title=t, kind="movie", year=2020, genres=["Drama"], score=s
        )
        for t, s in [("Alpha", 0.9), ("Beta", 0.8), ("Gamma", 0.7), ("Delta", 0.6)]
    ]
    prompt = build_compose_prompt("something good", ExecutionResult(items=items))
    assert "Alpha, Beta, Gamma, Delta" in prompt  # displayed order, verbatim
    assert prompt.index("Alpha") < prompt.index("Beta") < prompt.index("Gamma")


def test_chat_intent_kind_coercion() -> None:
    # A5: the planner's `kind` arg is a loose LLM value — synonyms map, unknown never raises.
    assert ChatIntent(kind="film").kind == "movie"
    assert ChatIntent(kind="TV series").kind == "show"
    assert ChatIntent(kind="série").kind == "show"
    assert ChatIntent(kind=42).kind is None  # garbage → no constraint, no error
    assert ChatIntent(kind="anything").kind is None
    assert ChatIntent().kind is None


def test_keyword_intent_detects_kind_fr_and_en() -> None:
    assert keyword_intent("a light movie for tonight").kind == "movie"
    assert keyword_intent("une série à binge ce soir").kind == "show"
    assert keyword_intent("something funny").kind is None  # no lean either way
    assert keyword_intent("movies and shows both fine").kind is None  # both → no constraint


def test_intent_filter_hard_filters_by_kind() -> None:
    # "a movie for tonight" must not return a show (review A5) — a hard filter, no fallback.
    def _cand(title: str, kind: str) -> Candidate:
        return Candidate(
            title_id=uuid.uuid4(),
            title=title,
            kind=kind,
            year=2020,
            genres=["Drama"],
            keywords=[],
            runtime_minutes=100,
            popularity=None,
            overview=None,
            similarity=0.5,
        )

    pool = [_cand("A Film", "movie"), _cand("A Series", "show")]
    kept = intent_filter(ChatIntent(kind="movie"))(pool)
    assert [c.title for c in kept] == ["A Film"]
    # No constraint → both survive.
    assert len(intent_filter(ChatIntent())(pool)) == 2


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
