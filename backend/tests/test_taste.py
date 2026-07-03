"""Taste extraction service + API (generate, view, edit, override persistence)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.taste import get_llm_provider
from phare.core.config import get_settings
from phare.db.models import EventType, Profile, TasteProfile, Title, TitleKind, User, WatchEvent
from phare.ingest.sample import seed_sample_data
from phare.providers.fakes import FakeLLMProvider
from phare.taste.service import (
    TasteService,
    _should_auto_refresh,
    effective_profile,
    localized_summary,
    maybe_refresh_taste,
    optional_llm_provider,
)
from tests.conftest import authed_client, make_account

CANNED = (
    '{"summary":"Likes cerebral sci-fi and prestige drama.",'
    '"likes":["slow-burn sci-fi"],"dislikes":["slapstick"],'
    '"hard_avoids":["gore"],"affinities":{"Science Fiction":0.9},'
    '"comfort_axis":"prestige drama","discovery_tolerance":0.6,"confidence":0.7}'
)


def _profile_with_history(session: Session) -> uuid.UUID:
    profile = Profile(display_name="me")
    session.add(profile)
    session.flush()
    seed_sample_data(session, profile.id)
    session.flush()
    return profile.id


def _account_with_history(session: Session) -> User:
    user = make_account(session)
    seed_sample_data(session, user.profile.id)
    session.flush()
    return user


def test_off_vocabulary_affinity_key_is_flagged(db_session: Session, caplog) -> None:
    # H1: an affinity key outside the closed vocabulary can't steer scoring. The profile is still
    # saved (never break on it), but the miss is surfaced instead of going silently inert.
    profile_id = _profile_with_history(db_session)
    canned = (
        '{"summary":"x","likes":[],"dislikes":[],"hard_avoids":[],'
        '"affinities":{"Underwater Basket Weaving":0.8},'
        '"comfort_axis":null,"discovery_tolerance":0.5,"confidence":0.6}'
    )
    llm = FakeLLMProvider(completion=canned)
    with caplog.at_level(logging.WARNING):
        taste = TasteService(db_session, llm, "test-model").generate(profile_id)
    assert taste.structured["affinities"]["Underwater Basket Weaving"] == 0.8  # saved + valid
    assert any(r.message == "taste_affinity.fallback" for r in caplog.records)


def test_localized_summary_translates_once_then_caches(db_session: Session) -> None:
    # F1: the summary is served in the request's language, translated on demand and cached so each
    # language costs at most one workhorse call.
    profile_id = _profile_with_history(db_session)
    taste = TasteService(
        db_session, FakeLLMProvider(completion=CANNED), "m", language="en"
    ).generate(profile_id)
    translator = FakeLLMProvider(completion="Vous aimez la SF cérébrale et le drame prestige.")
    first = localized_summary(db_session, taste, "fr", translator)
    assert first == "Vous aimez la SF cérébrale et le drame prestige."
    assert len(translator.prompts) == 1  # one workhorse call for the translation

    # A second French read is served from the cache — the (unused) provider is never called.
    unused = FakeLLMProvider(completion="SHOULD NOT BE CALLED")
    assert localized_summary(db_session, taste, "fr", unused) == first
    assert unused.prompts == []

    # The native language is returned as-is, with no translation call.
    assert localized_summary(db_session, taste, "en", None) == taste.summary_text


def test_localized_summary_offline_serves_native(db_session: Session) -> None:
    # Without an LLM (offline), a language miss falls back to the stored summary, not a failure.
    profile_id = _profile_with_history(db_session)
    taste = TasteService(
        db_session, FakeLLMProvider(completion=CANNED), "m", language="en"
    ).generate(profile_id)
    assert localized_summary(db_session, taste, "fr", None) == taste.summary_text


def test_degraded_taste_profile_is_marked_and_re_extracted(db_session: Session) -> None:
    # A14: a failed extraction fell back to the coarse genre-frequency profile and froze there. Now
    # it's marked degraded, the auto-refresh re-attempts it, and a success clears the flag — while
    # user overrides survive both transitions.
    profile_id = _profile_with_history(db_session)
    degraded = TasteService(db_session, FakeLLMProvider(completion="not json"), "m").generate(
        profile_id
    )
    assert degraded.degraded is True
    # A degraded profile forces a re-attempt regardless of how much history changed.
    assert _should_auto_refresh(db_session, profile_id, datetime.now(UTC)) is True

    degraded.user_overrides = {"likes": ["noir"]}
    db_session.flush()
    recovered = TasteService(db_session, FakeLLMProvider(completion=CANNED), "m").generate(
        profile_id
    )
    assert recovered.degraded is False  # marker lifted on a successful extraction
    assert recovered.user_overrides == {"likes": ["noir"]}  # hand edits survived


def test_generate_builds_profile(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=CANNED)

    taste = TasteService(db_session, llm, "test-model").generate(profile_id)

    assert taste.summary_text is not None
    assert taste.structured["affinities"]["Science Fiction"] == 0.9
    assert taste.confidence == 0.7
    assert taste.generated_at is not None
    # The prompt must actually include the user's history.
    assert "Dune" in llm.prompts[0]


# --- history roll-up (episode-level events collapse to one line per title) ---


_next_tmdb_id = iter(range(1, 1_000_000))


def _make_title(session: Session, name: str, *, kind: TitleKind = TitleKind.show) -> Title:
    title = Title(kind=kind, title=name, year=2013, genres=["Comedy"], tmdb_id=next(_next_tmdb_id))
    session.add(title)
    session.flush()
    return title


def _add_event(
    session: Session,
    profile_id: uuid.UUID,
    title: Title,
    *,
    when: datetime,
    type_: EventType = EventType.watched,
    rating: float | None = None,
) -> None:
    session.add(
        WatchEvent(
            profile_id=profile_id,
            title_id=title.id,
            type=type_,
            source="test",
            external_ref=f"{title.title}-{uuid.uuid4()}",
            occurred_at=when,
            rating=rating,
        )
    )
    session.flush()


def _history_lines(session: Session, profile_id: uuid.UUID) -> list[str]:
    return TasteService(session, FakeLLMProvider(completion=CANNED), "m")._history_lines(profile_id)


def test_history_rolls_episodes_into_one_line_with_count(db_session: Session) -> None:
    # Trakt imports are episode-level; a binged show must not eat dozens of the 150 lines.
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()
    show = _make_title(db_session, "Rick and Morty")
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for i in range(24):
        _add_event(db_session, profile.id, show, when=base + timedelta(hours=i))

    lines = _history_lines(db_session, profile.id)
    assert len(lines) == 1
    assert "Rick and Morty" in lines[0]
    assert "x24" in lines[0]  # count carried, not 24 separate lines


def test_history_shows_rating_when_any_event_rated(db_session: Session) -> None:
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()
    show = _make_title(db_session, "The Bear")
    base = datetime(2024, 1, 1, tzinfo=UTC)
    # Newest event carries the rating; older ones don't. Rating must still surface on the line.
    _add_event(db_session, profile.id, show, when=base, type_=EventType.watched)
    _add_event(
        db_session, profile.id, show, when=base + timedelta(days=1), type_=EventType.rated, rating=9
    )

    (line,) = _history_lines(db_session, profile.id)
    assert "rating=9" in line
    assert "x2" in line


def test_history_caps_at_taste_max_events_titles(db_session: Session) -> None:
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()
    cap = get_settings().taste_max_events
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for i in range(cap + 10):
        title = _make_title(db_session, f"Title {i:04d}", kind=TitleKind.movie)
        _add_event(db_session, profile.id, title, when=base + timedelta(hours=i))

    lines = _history_lines(db_session, profile.id)
    assert len(lines) == cap  # cap is on distinct titles, not raw events


def test_history_is_most_recent_first(db_session: Session) -> None:
    profile = Profile(display_name="me")
    db_session.add(profile)
    db_session.flush()
    old = _make_title(db_session, "Old Show", kind=TitleKind.movie)
    new = _make_title(db_session, "New Show", kind=TitleKind.movie)
    base = datetime(2024, 1, 1, tzinfo=UTC)
    _add_event(db_session, profile.id, old, when=base)
    _add_event(db_session, profile.id, new, when=base + timedelta(days=30))

    lines = _history_lines(db_session, profile.id)
    assert "New Show" in lines[0]
    assert "Old Show" in lines[1]


def test_generate_strips_reasoning_block(db_session: Session) -> None:
    # A reasoning model wraps its JSON in <think>…</think>; the answer must still parse.
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=f"<think>let me weigh the ratings…</think>\n{CANNED}")

    taste = TasteService(db_session, llm, "test-model").generate(profile_id)

    assert taste.structured["affinities"]["Science Fiction"] == 0.9
    assert taste.confidence == 0.7


def test_generate_falls_back_to_deterministic_when_unparseable(db_session: Session) -> None:
    # The model answered but emitted no JSON (e.g. spent its budget reasoning) — degrade, don't 500.
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion="<think>thinking forever, never answered</think>")

    taste = TasteService(db_session, llm, "test-model").generate(profile_id)

    assert taste.generated_at is not None
    assert taste.summary_text is not None and "lean toward" in taste.summary_text
    assert taste.structured["affinities"]  # derived from the sample history's genres
    assert taste.confidence <= 0.5  # marked as the coarse fallback it is


def test_deterministic_fallback_localises_summary(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion="<think>no json</think>")

    taste = TasteService(db_session, llm, "test-model", language="fr").generate(profile_id)

    assert taste.summary_text is not None and "vous penchez pour" in taste.summary_text


def test_generate_propagates_when_llm_call_raises(db_session: Session) -> None:
    # A *thrown* error (LLM unreachable) is not a parse failure — it must propagate so the ingest
    # auto-refresh can swallow it, rather than masking an outage behind a deterministic profile.
    profile_id = _profile_with_history(db_session)

    class _Boom:
        def complete(
            self, prompt: str, *, max_tokens: int | None = None, temperature: float | None = None
        ) -> str:
            raise RuntimeError("llm down")

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("llm down")

    with pytest.raises(RuntimeError):
        TasteService(db_session, _Boom(), "test-model").generate(profile_id)


def test_generate_localises_only_the_summary(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=CANNED)

    TasteService(db_session, llm, "test-model", language="fr").generate(profile_id)

    prompt = llm.prompts[0]
    assert "summary` field in French" in prompt  # the human-readable summary localises
    assert "in English" in prompt  # structured keys stay English so affinity matching holds


def test_maybe_refresh_taste_threads_request_language(db_session: Session) -> None:
    # The ingest paths (sample-data / source sync) auto-refresh taste with the *request* language.
    # Regression: they used to drop it, so a French user always got an English taste summary until
    # they hit "Regenerate" by hand.
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=CANNED)

    assert maybe_refresh_taste(db_session, profile_id, llm, "fr") is True
    assert any("summary` field in French" in prompt for prompt in llm.prompts)


def test_generate_preserves_user_overrides(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    service = TasteService(db_session, FakeLLMProvider(completion=CANNED), "m")

    taste = service.generate(profile_id)
    taste.user_overrides = {"hard_avoids": ["musicals"]}
    db_session.flush()

    service.generate(profile_id)  # regenerate
    db_session.refresh(taste)
    assert taste.user_overrides == {"hard_avoids": ["musicals"]}


def test_effective_profile_overrides_win(db_session: Session) -> None:
    taste = TasteProfile(
        profile_id=uuid.uuid4(),
        structured={"hard_avoids": ["gore"], "summary": "x"},
        user_overrides={"hard_avoids": ["musicals"]},
    )
    merged = effective_profile(taste)
    assert merged["hard_avoids"] == ["musicals"]
    assert merged["summary"] == "x"


def _client(session: Session, user: User, *, with_llm: bool = False) -> TestClient:
    overrides = {get_llm_provider: lambda: FakeLLMProvider(completion=CANNED)} if with_llm else None
    return authed_client(session, user, overrides=overrides)


def test_taste_api_404_before_generation(db_session: Session) -> None:
    user = _account_with_history(db_session)
    assert _client(db_session, user).get(f"/profiles/{user.profile.id}/taste").status_code == 404


def test_taste_api_generate_then_get_then_edit(db_session: Session) -> None:
    user = _account_with_history(db_session)
    profile_id = user.profile.id
    client = _client(db_session, user, with_llm=True)

    generated = client.post(f"/profiles/{profile_id}/taste/generate")
    assert generated.status_code == 200
    assert generated.json()["confidence"] == 0.7

    fetched = client.get(f"/profiles/{profile_id}/taste").json()
    assert fetched["structured"]["affinities"]["Science Fiction"] == 0.9

    edited = client.put(
        f"/profiles/{profile_id}/taste",
        json={"userOverrides": {"hard_avoids": ["musicals"]}},
    )
    assert edited.status_code == 200
    assert edited.json()["structured"]["hard_avoids"] == ["musicals"]


def test_generate_is_rate_limited_after_a_recent_generation(db_session: Session) -> None:
    user = _account_with_history(db_session)
    profile_id = user.profile.id
    client = _client(db_session, user, with_llm=True)

    first = client.post(f"/profiles/{profile_id}/taste/generate")
    assert first.status_code == 200

    # A second click right away must not spend another LLM call — it's on cooldown.
    second = client.post(f"/profiles/{profile_id}/taste/generate")
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert int(second.headers["Retry-After"]) > 0


def test_generate_without_llm_key_400(db_session: Session) -> None:
    user = _account_with_history(db_session)
    # No get_llm_provider override and no LLM_API_KEY configured -> 400.
    assert (
        _client(db_session, user).post(f"/profiles/{user.profile.id}/taste/generate").status_code
        == 400
    )


class _RaisingLLMProvider(FakeLLMProvider):
    """An LLM provider whose ``complete`` raises — to exercise the manual-generate transport/budget
    guard without a real network call (tests stay hermetic)."""

    def __init__(self, error: Exception) -> None:
        super().__init__(completion="unused")
        self._error = error

    def complete(self, prompt: str, *, max_tokens=None, temperature=None) -> str:  # noqa: ARG002
        raise self._error


def _client_with_raising_llm(session: Session, user: User, error: Exception) -> TestClient:
    return authed_client(
        session, user, overrides={get_llm_provider: lambda: _RaisingLLMProvider(error)}
    )


def _spy_fallback(monkeypatch) -> list[tuple[str, str]]:
    """Record every ``record_fallback(component, reason, …)`` call the endpoint makes. The app's
    startup reconfigures the root logger (clears caplog's handler), so spy on the call directly
    rather than fish it out of captured logs."""
    calls: list[tuple[str, str]] = []
    import phare.api.taste as taste_module

    def _record(component: str, reason: str, **fields) -> None:  # noqa: ARG001
        calls.append((component, reason))

    monkeypatch.setattr(taste_module, "record_fallback", _record)
    return calls


def test_manual_generate_returns_503_on_llm_transport_failure(
    db_session: Session, monkeypatch
) -> None:
    # F3: the manual "Regenerate" button explicitly asks for an LLM extraction. A transport failure
    # (403/429/5xx/network) must surface a structured 503 — not a 500, and not a silent fall back to
    # the deterministic profile — and it must announce itself via record_fallback (metric G1).
    user = _account_with_history(db_session)
    fallbacks = _spy_fallback(monkeypatch)
    import httpx

    request = httpx.Request("POST", "https://llm.example/chat/completions")
    response = httpx.Response(503, request=request)
    client = _client_with_raising_llm(
        db_session, user, httpx.HTTPStatusError("boom", request=request, response=response)
    )
    resp = client.post(f"/profiles/{user.profile.id}/taste/generate")
    assert resp.status_code == 503
    body = resp.json()["detail"]
    assert body["code"] == "llm_unavailable"
    assert body["reason"] == "llm_unreachable"
    assert ("taste_extraction", "llm_unreachable") in fallbacks
    # No taste profile was written on the failure (the user's existing profile stays as-is).
    assert _stored_taste(db_session, user.profile.id) is None


def test_manual_generate_returns_503_on_budget_exhausted(db_session: Session, monkeypatch) -> None:
    # F3: the monthly LLM token budget being spent is also an honest 503 (distinct reason; not 500).
    from phare.core.llm_budget import LLMBudgetExceeded

    user = _account_with_history(db_session)
    fallbacks = _spy_fallback(monkeypatch)
    client = _client_with_raising_llm(db_session, user, LLMBudgetExceeded("budget spent"))
    resp = client.post(f"/profiles/{user.profile.id}/taste/generate")
    assert resp.status_code == 503
    body = resp.json()["detail"]
    assert body["code"] == "llm_unavailable"
    assert body["reason"] == "budget_exhausted"
    assert ("taste_extraction", "budget_exhausted") in fallbacks


def test_manual_generate_still_degrades_on_unparseable_json(db_session: Session) -> None:
    # Regression guard: an *unparseable completion* (the model answered, but not with JSON) must NOT
    # 503 — generate() still degrades to the deterministic genre-frequency profile (200), unchanged.
    user = _account_with_history(db_session)
    client = authed_client(
        db_session,
        user,
        overrides={get_llm_provider: lambda: FakeLLMProvider(completion="not json")},
    )
    resp = client.post(f"/profiles/{user.profile.id}/taste/generate")
    assert resp.status_code == 200
    stored = _stored_taste(db_session, user.profile.id)
    assert stored is not None and stored.degraded is True


# --- automatic taste refresh on ingest --------------------------------------


def _stored_taste(session: Session, profile_id: uuid.UUID) -> TasteProfile | None:
    return session.scalar(select(TasteProfile).where(TasteProfile.profile_id == profile_id))


def test_maybe_refresh_taste_generates_with_llm(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    assert maybe_refresh_taste(db_session, profile_id, FakeLLMProvider(completion=CANNED)) is True
    taste = _stored_taste(db_session, profile_id)
    assert taste is not None and taste.summary_text is not None


def test_maybe_refresh_taste_noops_offline(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    # No LLM configured -> no taste written, deterministic engine still personalises elsewhere.
    assert maybe_refresh_taste(db_session, profile_id, None) is False
    assert _stored_taste(db_session, profile_id) is None


def test_maybe_refresh_taste_swallows_llm_failure(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)

    class _Boom:
        def complete(
            self, prompt: str, *, max_tokens: int | None = None, temperature: float | None = None
        ) -> str:
            raise RuntimeError("llm down")

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("llm down")

    # A taste failure must never break ingestion.
    assert maybe_refresh_taste(db_session, profile_id, _Boom()) is False
    assert _stored_taste(db_session, profile_id) is None


def _add_events(session: Session, profile_id: uuid.UUID, count: int) -> None:
    """Append ``count`` fresh watch events so the auto-refresh gate sees drift.

    ``ingested_at`` is stamped explicitly because Postgres freezes ``now()`` at transaction start,
    and the whole test runs in one transaction — so the server default would land *before* the
    taste's ``generated_at`` and the gate wouldn't see these as new.
    """
    title = session.scalar(select(Title))
    assert title is not None
    now = datetime.now(UTC)
    for i in range(count):
        session.add(
            WatchEvent(
                profile_id=profile_id,
                title_id=title.id,
                type=EventType.watched,
                source="test",
                external_ref=f"gate-{uuid.uuid4()}-{i}",
                ingested_at=now,
            )
        )
    session.flush()


def test_auto_refresh_skips_when_nothing_changed(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=CANNED)
    # First call generates (no profile yet); the immediate second call has zero new events → skip.
    assert maybe_refresh_taste(db_session, profile_id, llm) is True
    generated_at = _stored_taste(db_session, profile_id).generated_at
    assert maybe_refresh_taste(db_session, profile_id, llm) is False
    assert len(llm.prompts) == 1  # the gate spared a second full extraction
    assert _stored_taste(db_session, profile_id).generated_at == generated_at


def test_auto_refresh_runs_after_enough_new_events(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=CANNED)
    assert maybe_refresh_taste(db_session, profile_id, llm) is True
    _add_events(db_session, profile_id, get_settings().taste_refresh_min_events)
    # Enough drift since the last generation → worth a re-read.
    assert maybe_refresh_taste(db_session, profile_id, llm) is True
    assert len(llm.prompts) == 2


def test_auto_refresh_folds_in_trickle_once_stale(db_session: Session) -> None:
    profile_id = _profile_with_history(db_session)
    llm = FakeLLMProvider(completion=CANNED)
    assert maybe_refresh_taste(db_session, profile_id, llm) is True
    # A single new event isn't enough on its own...
    _add_events(db_session, profile_id, 1)
    assert maybe_refresh_taste(db_session, profile_id, llm) is False
    # ...but once the profile is older than the interval, the trickle gets folded in.
    taste = _stored_taste(db_session, profile_id)
    interval = get_settings().taste_refresh_min_interval_seconds
    taste.generated_at = datetime.now(UTC) - timedelta(seconds=interval + 60)
    db_session.flush()
    assert maybe_refresh_taste(db_session, profile_id, llm) is True


def test_optional_llm_provider_none_without_key() -> None:
    assert optional_llm_provider() is None


def test_sample_data_auto_generates_taste(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user never asks for taste — loading data generates it automatically.
    monkeypatch.setattr(
        "phare.api.profiles.optional_llm_provider",
        lambda: FakeLLMProvider(completion=CANNED),
    )
    user = make_account(db_session, display_name="auto")
    profile_id = user.profile.id
    client = _client(db_session, user)

    assert client.post(f"/profiles/{profile_id}/sample-data").status_code == 200

    fetched = client.get(f"/profiles/{profile_id}/taste")
    assert fetched.status_code == 200
    assert fetched.json()["summary"] is not None
