"""Agent tool layer: planner parsing, tool execution, undo, distillation, and the service turn."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.agent import memory as memory_store
from phare.agent import planner
from phare.agent import tools as agent_tools
from phare.agent.schema import AgentPlan, ToolCall
from phare.agent.service import ChatService, build_compose_prompt
from phare.agent.tools import ToolContext, _as_uuid, execute_plan, undo_action
from phare.db.models import (
    CommitmentStatus,
    EventType,
    Profile,
    RecommendationLog,
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


def test_planner_pins_temperature_to_zero_for_determinism() -> None:
    # Planning is mechanical tool-selection, not prose: pinning temperature to 0 stops a reasoning
    # model from sampling a different plan for the same message (the genre filter that lands one
    # turn and vanishes the next). Regression guard for that flakiness.
    llm = FakeLLMProvider(completion='{"calls":[]}')
    planner.plan(_DummySession(), uuid.uuid4(), "something funny and short", llm, now=_NOW)
    assert llm.temperatures == [0.0]


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


def test_log_signal_resolves_exact_popular_title_over_obscure_near_name(
    db_session: Session,
) -> None:
    # Review H3b: "loved Get Out" logged onto "The Get Out" (2026, obscure) instead of Get Out
    # (2017). Exact-name + popularity now resolves the right one, and the summary names it + year.
    profile_id = _seed(db_session)
    get_out = Title(kind=TitleKind.movie, tmdb_id=301, title="Get Out", year=2017, vote_count=8000)
    decoy = Title(kind=TitleKind.movie, tmdb_id=302, title="The Get Out", year=2026, vote_count=30)
    db_session.add_all([get_out, decoy])
    db_session.flush()
    result = _run(db_session, profile_id, "log_signal", {"title": "Get Out", "signal": "loved"})

    events = db_session.scalars(select(WatchEvent).where(WatchEvent.profile_id == profile_id)).all()
    assert events and all(e.title_id == get_out.id for e in events)
    assert any("Get Out" in a.summary and "2017" in a.summary for a in result.actions)


def test_log_signal_prefers_a_recently_recommended_title(db_session: Session) -> None:
    # Two films both named "Solaris"; the more-voted one would win a blind search, but the agent
    # just recommended the 1972 one in chat — conversation context wins (review H3b).
    profile_id = _seed(db_session)
    old = Title(kind=TitleKind.movie, tmdb_id=401, title="Solaris", year=1972, vote_count=2000)
    new = Title(kind=TitleKind.movie, tmdb_id=402, title="Solaris", year=2002, vote_count=5000)
    db_session.add_all([old, new])
    db_session.flush()
    db_session.add(
        RecommendationLog(
            profile_id=profile_id, title_id=old.id, row_key="chat", rank=0, source="chat"
        )
    )
    db_session.flush()
    result = _run(db_session, profile_id, "log_signal", {"title": "Solaris", "signal": "loved"})

    events = db_session.scalars(select(WatchEvent).where(WatchEvent.profile_id == profile_id)).all()
    assert events and all(e.title_id == old.id for e in events)  # the recommended 1972, not 2002
    assert result.actions and result.actions[0].kind == "logged_signal"


def test_log_signal_ambiguous_asks_instead_of_writing(db_session: Session) -> None:
    # Two equally-plausible titles (same name, similar popularity) and no conversation context: the
    # tool must NOT guess — it writes nothing and asks which one (review H3b).
    profile_id = _seed(db_session)
    db_session.add_all(
        [
            Title(kind=TitleKind.movie, tmdb_id=501, title="The Thing", year=1982, vote_count=3000),
            Title(kind=TitleKind.movie, tmdb_id=502, title="The Thing", year=2011, vote_count=2900),
        ]
    )
    db_session.flush()
    result = _run(db_session, profile_id, "log_signal", {"title": "The Thing", "signal": "loved"})

    assert (
        db_session.scalars(select(WatchEvent).where(WatchEvent.profile_id == profile_id)).all()
        == []
    )
    assert result.actions == []
    assert any("The Thing" in n for n in result.notes)


def test_tool_exception_surfaces_to_the_composer(db_session: Session, monkeypatch) -> None:
    # B3: a tool that raises used to be swallowed, so the composer could still say "done!". Now the
    # failure lands in failed_calls and is fed to the composer prompt to keep the reply honest.
    profile_id = _seed(db_session)

    def _boom(ctx, args, result):  # noqa: ANN001, ARG001
        raise RuntimeError("kaboom")

    monkeypatch.setitem(agent_tools._TOOLS, "boom", _boom)
    result = _run(db_session, profile_id, "boom", {})

    assert result.failed_calls  # not silently swallowed
    prompt = build_compose_prompt("hi", result)
    assert "boom" in prompt  # the failed action is named for the composer


def test_undo_ignores_a_malformed_ref_instead_of_targeting_the_zero_uuid(
    db_session: Session,
) -> None:
    # G4: _as_uuid used to coerce a bad ref to UUID(int=0), silently aiming at a non-existent row.
    assert _as_uuid("not-a-uuid") is None
    assert _as_uuid(str(uuid.uuid4())) is not None
    profile_id = _seed(db_session)
    assert undo_action(db_session, profile_id, "event:garbage") is False  # clean no-op, no crash


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
        def complete(
            self, prompt: str, *, max_tokens: int | None = None, temperature: float | None = None
        ) -> str:
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

    def complete(
        self, prompt: str, *, max_tokens: int | None = None, temperature: float | None = None
    ) -> str:
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


def test_explain_picks_resurfaces_the_last_chat_slate(db_session: Session) -> None:
    from phare.recommend.log import log_chat
    from phare.recommend.schema import Recommendation

    profile_id = _seed(db_session)
    titles = db_session.scalars(select(Title).order_by(Title.title)).all()[:2]
    log_chat(
        db_session,
        profile_id,
        [
            Recommendation(
                title_id=t.id, title=t.title, kind=t.kind.value, year=2020, genres=[], score=1.0
            )
            for t in titles
        ],
    )

    result = _run(db_session, profile_id, "explain_picks", {})

    assert {i.title for i in result.items} == {t.title for t in titles}
    assert result.suppress_logging is True  # already-logged picks aren't re-logged


def test_explain_picks_with_no_prior_slate_notes_it(db_session: Session) -> None:
    profile_id = _seed(db_session)
    result = _run(db_session, profile_id, "explain_picks", {})
    assert result.items == []
    assert any("no earlier picks" in note for note in result.notes)


def test_recommend_coerces_a_bare_string_genre_arg(db_session: Session) -> None:
    # A model that returns include_genres as the bare string "comedy" (not ["comedy"]) must not
    # iterate it into ['c','o','m','e','d','y'] — that silently disabled the genre filter.
    profile_id = _seed(db_session)
    result = _run(db_session, profile_id, "recommend", {"include_genres": "comedy"})
    assert result.intent.include_genres == ["comedy"]


def test_recommend_tolerates_mood_as_a_list(db_session: Session) -> None:
    # The live failure temperature=0 surfaced: a model returned mood as ["lighthearted","relaxing"],
    # which crashed ChatIntent (mood: str) and sank the whole recommend tool. It's coerced now.
    profile_id = _seed(db_session)
    result = _run(
        db_session,
        profile_id,
        "recommend",
        {"mood": ["lighthearted", "relaxing"], "max_runtime": 90},
    )
    assert result.intent.mood == "lighthearted, relaxing"
    assert result.intent.max_runtime == 90
    assert result.intent.include_genres == []  # absent arg stays an empty list, not a crash


def test_runtime_capped_recommend_drives_the_lazy_runtime_backfill(db_session: Session) -> None:
    # Wiring guard: a runtime-capped recommend must drive the pool runtime backfill
    # (tool_recommend -> recommend(runtime_source) -> _enrich_runtimes), so "something short" has
    # data to filter on — and a plain recommend must NOT touch the provider (no wasted calls).
    from phare.providers.fakes import FakeMetadataProvider
    from phare.providers.types import TitleMetadata

    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()
    titles = []
    for n, name in enumerate(["Dune", "Arrival", "Sicario", "Heat", "Tenet"]):
        title = Title(
            kind=TitleKind.movie, tmdb_id=100 + n, title=name, genres=["Drama"], vote_count=50 - n
        )
        db_session.add(title)
        titles.append(title)
    db_session.flush()
    EmbeddingService(db_session, LocalHashEmbeddingProvider(), LOCAL_MODEL_VERSION).embed_missing()
    # A liked title gives the profile a taste centroid, so recommend actually returns a pool.
    db_session.add(
        WatchEvent(
            profile_id=profile.id, title_id=titles[0].id, type=EventType.liked, source="test"
        )
    )
    db_session.flush()

    def _ctx_with(metadata: object) -> ToolContext:
        return ToolContext(
            session=db_session,
            profile_id=profile.id,
            recommender=_recommender(db_session),
            now=_NOW,
            metadata=metadata,  # type: ignore[arg-type]  # fake satisfies get_title at runtime
        )

    provider = FakeMetadataProvider(
        titles={
            (t.tmdb_id, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie, title=t.title, runtime_minutes=95
            )
            for t in titles
        }
    )
    execute_plan(
        _ctx_with(provider),
        AgentPlan(calls=[ToolCall(tool="recommend", args={"max_runtime": 120})]),
    )
    db_session.flush()
    assert provider.calls  # the runtime cap drove get_title...
    assert any(t.runtime_minutes == 95 for t in titles)  # ...and the pool's runtimes were persisted

    # No runtime cap → the backfill must not fire.
    plain = FakeMetadataProvider(titles={})
    execute_plan(_ctx_with(plain), AgentPlan(calls=[ToolCall(tool="recommend", args={})]))
    assert plain.calls == []


def test_chat_intent_coerces_loose_llm_arg_shapes() -> None:
    from phare.agent.schema import ChatIntent

    loose = ChatIntent(include_genres="comedy", mood=["light", "funny"], max_runtime="90 minutes")
    assert loose.include_genres == ["comedy"]
    assert loose.mood == "light, funny"
    assert loose.max_runtime == 90

    empty = ChatIntent(include_genres=None, mood="", max_runtime=None)
    assert empty.include_genres == [] and empty.mood is None and empty.max_runtime is None


def test_unusable_plan_falls_back_to_general_picks_not_a_no_match(db_session: Session) -> None:
    # The failure this fixes: an in-scope watch request whose plan produced no usable recommend
    # (a flaky/empty reasoning-model plan) used to answer "I couldn't find a match" — reading as an
    # empty catalog. It must fall back to a general slate and flag the turn degraded instead.
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
    # A plan whose only call is an unknown tool: parses fine, runs, but produces nothing at all.
    workhorse = FakeLLMProvider(completion='{"calls":[{"tool":"noop","args":{}}]}')
    agent = FakeLLMProvider(completion="Here are a few general ideas you might like.")
    recommender = RecommendationService(
        db_session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=workhorse,
    )

    reply = ChatService(recommender, agent).respond(
        profile_id, "something funny and short", now=_NOW
    )

    assert reply.items  # a general slate, not an empty apology
    assert reply.degraded is True  # surfaced honestly as reduced mode


def test_chat_surfaces_a_tool_note_instead_of_an_unrelated_slate(db_session: Session) -> None:
    # The fallback above must NOT trample a real outcome note: "why these?" with no prior slate
    # should still tell the user there's nothing to explain, not bury it under random picks.
    profile_id = _seed(db_session)
    workhorse = FakeLLMProvider(completion='{"calls":[{"tool":"explain_picks","args":{}}]}')
    agent = FakeLLMProvider(completion="SHOULD NOT BE USED")
    recommender = RecommendationService(
        db_session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=workhorse,
    )

    reply = ChatService(recommender, agent).respond(profile_id, "why these?", now=_NOW)

    assert reply.items == []  # no unrelated slate conjured
    assert "earlier picks" in reply.reply_text.lower()  # the honest note is surfaced
    assert agent.prompts == []  # answered deterministically, no agent-model spend
