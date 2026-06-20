"""Agent tool layer: planner parsing, tool execution, undo, distillation, and the service turn."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.agent import memory as memory_store
from phare.agent import planner
from phare.agent.schema import AgentPlan, ToolCall
from phare.agent.service import ChatService
from phare.agent.tools import ToolContext, execute_plan, undo_action
from phare.db.models import (
    CommitmentStatus,
    EventType,
    Profile,
    TasteProfile,
    Title,
    TitleKind,
    WatchCommitment,
    WatchEvent,
)
from phare.embeddings.service import EmbeddingService
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider
from phare.recommend.service import RecommendationService

_NOW = datetime(2026, 6, 20, tzinfo=UTC)


def _recommender(session: Session) -> RecommendationService:
    return RecommendationService(
        session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=None,
    )


def _ctx(session: Session, profile_id: uuid.UUID) -> ToolContext:
    return ToolContext(
        session=session,
        profile_id=profile_id,
        recommender=_recommender(session),
        now=_NOW,
        metadata=None,  # local title resolution (ilike) — no TMDB in tests
    )


def _seed(session: Session) -> uuid.UUID:
    profile = Profile(display_name="me")
    session.add(profile)
    session.flush()
    for n, name in enumerate(["Dune", "Arrival", "Sicario", "Heat"]):
        session.add(Title(kind=TitleKind.movie, tmdb_id=n + 1, title=name, genres=["Drama"]))
    session.flush()
    EmbeddingService(session, LocalHashEmbeddingProvider(), LOCAL_MODEL_VERSION).embed_missing()
    return profile.id


def _run(session: Session, profile_id: uuid.UUID, tool: str, args: dict):
    plan = AgentPlan(calls=[ToolCall(tool=tool, args=args)])
    return execute_plan(_ctx(session, profile_id), plan)


# --- planner -----------------------------------------------------------------


def test_planner_parses_tool_calls() -> None:
    llm = FakeLLMProvider(
        completion='{"calls":[{"tool":"log_signal","args":{"title":"Dune","signal":"loved"}}]}'
    )
    plan = planner.plan(_DummySession(), uuid.uuid4(), "I saw Dune and loved it", llm, now=_NOW)
    assert [c.tool for c in plan.calls] == ["log_signal"]
    assert plan.calls[0].args["title"] == "Dune"


def test_planner_bad_json_falls_back_to_recommend() -> None:
    plan = planner.plan(
        _DummySession(), uuid.uuid4(), "hi", FakeLLMProvider(completion="not json"), now=_NOW
    )
    assert [c.tool for c in plan.calls] == ["recommend"]


def test_planner_explicit_empty_calls_is_an_off_topic_decline() -> None:
    # `{"calls": []}` is the planner declining off-topic — kept empty (not coerced to recommend),
    # so the service can answer with a template instead of spending the agent model.
    plan = planner.plan(
        _DummySession(),
        uuid.uuid4(),
        "write me code",
        FakeLLMProvider(completion='{"calls":[]}'),
        now=_NOW,
    )
    assert plan.calls == []


def test_planner_missing_calls_key_falls_back_to_recommend() -> None:
    # A response that doesn't follow the contract (no "calls" key) is a malformed plan, not a
    # deliberate decline → default to recommend so a real request never silently does nothing.
    plan = planner.plan(
        _DummySession(), uuid.uuid4(), "something good", FakeLLMProvider(completion="{}"), now=_NOW
    )
    assert [c.tool for c in plan.calls] == ["recommend"]


class _DummySession:
    """The planner only reads memory/taste for context; an empty profile returns nothing."""

    def scalar(self, *_a: object, **_k: object) -> None:
        return None

    def scalars(self, *_a: object, **_k: object) -> list[object]:
        return []

    def get(self, *_a: object, **_k: object) -> None:
        return None


# --- tools -------------------------------------------------------------------


def test_log_signal_writes_events_and_excludes_title(db_session: Session) -> None:
    profile_id = _seed(db_session)
    result = _run(db_session, profile_id, "log_signal", {"title": "Dune", "signal": "loved"})

    events = db_session.scalars(select(WatchEvent).where(WatchEvent.profile_id == profile_id)).all()
    assert {e.type for e in events} == {EventType.watched, EventType.liked}
    assert all(e.source == "chat" for e in events)
    assert len(result.actions) == 1 and result.actions[0].kind == "logged_signal"

    # Dune now has events → excluded from candidates.
    items = _recommender(db_session).recommend(profile_id)
    assert all(i.title != "Dune" for i in items)


def test_log_signal_unresolvable_writes_nothing(db_session: Session) -> None:
    profile_id = _seed(db_session)
    result = _run(db_session, profile_id, "log_signal", {"title": "Zxqyt", "signal": "loved"})

    assert db_session.scalars(select(WatchEvent)).all() == []
    assert result.actions == []
    assert any("couldn't find" in n for n in result.notes)


def test_commitment_and_resolution_flow(db_session: Session) -> None:
    profile_id = _seed(db_session)
    _run(db_session, profile_id, "set_commitment", {"title": "Sicario"})
    pending = db_session.scalars(
        select(WatchCommitment).where(WatchCommitment.status == CommitmentStatus.pending)
    ).all()
    assert len(pending) == 1

    _run(
        db_session,
        profile_id,
        "resolve_commitment",
        {"title": "Sicario", "outcome": "watched", "reaction": "loved it"},
    )
    commitment = db_session.get(WatchCommitment, pending[0].id)
    assert commitment is not None and commitment.status is CommitmentStatus.watched
    # A reaction with positive words → watched + liked (loved).
    types = {
        e.type for e in db_session.scalars(select(WatchEvent).where(WatchEvent.source == "chat"))
    }
    assert EventType.liked in types


def test_remember_and_update_taste(db_session: Session) -> None:
    profile_id = _seed(db_session)
    _run(db_session, profile_id, "remember", {"text": "no gore", "kind": "preference"})
    _run(db_session, profile_id, "update_taste", {"add_avoid": ["gore"]})

    notes = memory_store.list_notes(db_session, profile_id)
    assert [n.text for n in notes] == ["no gore"]
    taste = db_session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))
    assert taste is not None and taste.user_overrides["hard_avoids"] == ["gore"]


# --- undo --------------------------------------------------------------------


def test_undo_reverses_signal_and_taste(db_session: Session) -> None:
    profile_id = _seed(db_session)
    signal = _run(db_session, profile_id, "log_signal", {"title": "Dune", "signal": "loved"})
    taste = _run(db_session, profile_id, "update_taste", {"add_avoid": ["gore"]})

    assert undo_action(db_session, profile_id, signal.actions[0].undo_token or "")
    assert undo_action(db_session, profile_id, taste.actions[0].undo_token or "")
    db_session.flush()

    assert db_session.scalars(select(WatchEvent)).all() == []
    profile_taste = db_session.scalar(
        select(TasteProfile).where(TasteProfile.profile_id == profile_id)
    )
    assert profile_taste is not None and profile_taste.user_overrides["hard_avoids"] == []


# --- distillation: notes feed taste extraction -------------------------------


def test_memory_notes_feed_taste_extraction_prompt(db_session: Session) -> None:
    from phare.taste.service import maybe_refresh_taste

    profile_id = _seed(db_session)
    memory_store.create_note(db_session, profile_id, "watching with my kid this month")
    db_session.flush()
    canned = (
        '{"summary":"x","likes":[],"dislikes":[],"hard_avoids":[],"affinities":{},'
        '"comfort_axis":null,"discovery_tolerance":0.5,"confidence":0.5}'
    )
    llm = FakeLLMProvider(completion=canned)

    maybe_refresh_taste(db_session, profile_id, llm)
    assert any("watching with my kid" in p for p in llm.prompts)


# --- cost discipline + scope -------------------------------------------------


def test_chat_recommend_turn_is_cost_bounded(db_session: Session) -> None:
    # A recommend turn must hit the big agent model once (the reply) and the workhorse once (the
    # plan) — never a per-item explanation call (those are templated in chat).
    profile_id = _seed(db_session)
    dune = db_session.scalar(select(Title).where(Title.title == "Dune"))
    assert dune is not None
    db_session.add(
        WatchEvent(
            profile_id=profile_id,
            title_id=dune.id,
            type=EventType.watched,
            source="t",
            external_ref="seed",
            occurred_at=_NOW,
        )
    )
    db_session.flush()

    workhorse = FakeLLMProvider(completion='{"calls":[{"tool":"recommend","args":{}}]}')
    agent = FakeLLMProvider(completion="A few ideas you might enjoy!")
    recommender = RecommendationService(
        db_session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=workhorse,
    )

    reply = ChatService(recommender, agent).respond(profile_id, "something to watch", now=_NOW)

    assert reply.items  # got picks (Dune excluded as watched)
    assert len(agent.prompts) == 1  # the big agent model is used only for the reply
    assert len(workhorse.prompts) == 1  # planner only — per-item explanations are templated


def test_off_topic_turn_declines_without_spending_the_agent_model(db_session: Session) -> None:
    # The planner declines (empty plan) → the reply is a deterministic steer-back, and the big
    # agent model is never called. This is also the prompt-injection-probe path, so it must be free.
    profile_id = _seed(db_session)
    workhorse = FakeLLMProvider(completion='{"calls":[]}')
    agent = FakeLLMProvider(completion="SHOULD NOT BE USED")
    recommender = RecommendationService(
        db_session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=workhorse,
    )

    reply = ChatService(recommender, agent).respond(profile_id, "write me a poem", now=_NOW)

    assert reply.items == [] and reply.actions == []
    assert "watch" in reply.reply_text.lower()  # steered back to movies/TV
    assert agent.prompts == []  # the big model was never spent on the decline
    assert len(workhorse.prompts) == 1  # only the planner ran


def test_agent_prompts_are_scoped_to_movies_and_tv() -> None:
    from phare.agent.planner import _SYSTEM
    from phare.agent.service import _COMPOSE_SYSTEM

    for prompt in (_SYSTEM, _COMPOSE_SYSTEM):
        low = prompt.lower()
        assert "movie" in low and "tv" in low
        assert "only" in low  # scoped to the domain
    assert "empty calls" in _SYSTEM.lower()  # planner declines off-topic by doing nothing
    assert "decline" in _COMPOSE_SYSTEM.lower()  # composer politely declines off-topic


# --- composer: natural-language reply with a template fallback ---------------


def test_compose_reply_uses_model_text_and_falls_back() -> None:
    from phare.agent.schema import AgentAction
    from phare.agent.service import _compose_reply_llm
    from phare.agent.tools import ExecutionResult

    result = ExecutionResult(actions=[AgentAction(kind="logged_signal", summary="logged Dune")])

    ok = _compose_reply_llm(FakeLLMProvider(completion="Lovely — noted!"), "msg", result)
    assert ok == "Lovely — noted!"

    class _Boom:
        def complete(self, prompt: str, *, max_tokens: int | None = None) -> str:
            raise RuntimeError("down")

        def embed(self, texts: object) -> object:  # pragma: no cover
            raise NotImplementedError

    fell_back = _compose_reply_llm(_Boom(), "msg", result)
    assert "logged Dune" in fell_back  # deterministic template


# --- service turn (planner → tools → reply) ----------------------------------


class _RoutingLLM:
    """Returns a tool plan for the planner prompt, a taste JSON otherwise; ignores embeddings."""

    def __init__(self, plan_json: str) -> None:
        self.plan_json = plan_json
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, max_tokens: int | None = None) -> str:
        self.prompts.append(prompt)
        if "planner" in prompt.lower():
            return self.plan_json
        if "chat assistant" in prompt.lower():  # the composer prompt
            return "Nice — noted that for you."
        return (
            '{"summary":"x","likes":[],"dislikes":[],"hard_avoids":[],"affinities":{},'
            '"comfort_axis":null,"discovery_tolerance":0.5,"confidence":0.5}'
        )

    def embed(self, texts: object) -> list[list[float]]:  # pragma: no cover - unused
        raise NotImplementedError


def test_service_tool_turn_logs_signal_and_confirms(db_session: Session) -> None:
    profile_id = _seed(db_session)
    llm = _RoutingLLM('{"calls":[{"tool":"log_signal","args":{"title":"Dune","signal":"loved"}}]}')
    service = ChatService(_recommender(db_session), chat_llm=llm)

    reply = service.respond(profile_id, "I already saw Dune and loved it", now=_NOW)

    assert reply.actions and reply.actions[0].kind == "logged_signal"
    assert "Dune" in reply.actions[0].summary  # the write is recorded deterministically
    assert reply.reply_text  # the agent model wrote a natural reply
    assert any(
        e.source == "chat"
        for e in db_session.scalars(select(WatchEvent).where(WatchEvent.profile_id == profile_id))
    )
