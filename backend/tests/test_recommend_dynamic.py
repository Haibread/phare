"""Dynamic themed rows: theme selection (offline + LLM) and DB-backed row building."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from phare.api.deps import Embedder, get_embedder, get_optional_chat_llm
from phare.catalog.sample import seed_sample_catalog
from phare.db.models import ROW_KEY_MAX_LEN, Profile, RecommendationLog
from phare.ingest.sample import seed_sample_data
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider
from phare.providers.http import TTLCache
from phare.recommend.dynamic import _slug, dynamic_rows, propose_themes
from phare.recommend.service import RecommendationService
from tests.conftest import authed_client, make_account

_OCTOBER = datetime(2026, 10, 15, tzinfo=UTC)
_MARCH = datetime(2026, 3, 15, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_theme_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Themes are cached process-wide per (taste, day); seeded profiles share an empty-taste
    # fingerprint, so give each test an isolated cache to avoid cross-test bleed.
    monkeypatch.setattr("phare.recommend.dynamic._THEME_CACHE", TTLCache(ttl=300))


def test_fallback_uses_season_taste_and_discovery() -> None:
    taste = {"affinities": {"Science Fiction": 0.9, "Musical": -0.5}}
    themes, degraded = propose_themes(taste, _OCTOBER, llm=None)
    assert degraded is False  # no LLM is the honest offline path, not degradation
    keys = [t.key for t in themes]

    assert any("Horror" in t.include_genres for t in themes)  # October -> spooky season
    assert any(t.include_genres == ["Science Fiction"] for t in themes)  # top positive affinity
    assert "dyn:something-different" in keys  # discovery row always reserved
    assert len(themes) <= 3


def test_fallback_without_season_still_has_taste_and_discovery() -> None:
    themes, _ = propose_themes({"affinities": {"Drama": 0.8}}, _MARCH, llm=None)
    assert any(t.include_genres == ["Drama"] for t in themes)
    assert any(t.swing_slots >= 3 for t in themes)  # the discovery row


def test_llm_themes_are_parsed() -> None:
    completion = (
        '[{"title":"Cozy mysteries","genres":["Mystery"]},{"title":"Wildcard","genres":[]}]'
    )
    llm = FakeLLMProvider(completion=completion)
    themes, degraded = propose_themes({"summary": "x"}, _MARCH, llm=llm)
    assert [t.title for t in themes] == ["Cozy mysteries", "Wildcard"]
    assert themes[0].include_genres == ["Mystery"]
    assert degraded is False  # the model produced usable themes


def test_llm_theme_prompt_localises_titles_but_keeps_genres_english() -> None:
    llm = FakeLLMProvider(completion='[{"title":"X","genres":[]}]')
    propose_themes({"summary": "x"}, _MARCH, llm=llm, language="fr")
    prompt = llm.prompts[0]
    assert "French" in prompt  # titles localised
    assert "Genre names MUST stay in English" in prompt  # genres key the catalog


def test_slug_truncates_long_titles_but_stays_distinct() -> None:
    long_a = (
        "Late-October horror you'd actually tolerate on a quiet school night with the lights on"
    )
    long_b = long_a + " and popcorn"
    key_a, key_b = _slug(long_a), _slug(long_b)

    assert len(key_a) <= ROW_KEY_MAX_LEN
    assert len(key_b) <= ROW_KEY_MAX_LEN
    assert key_a.startswith("dyn:")
    assert key_a != key_b  # distinct titles keep distinct keys via the hash suffix
    assert _slug(long_a) == key_a  # stable


def test_llm_failure_falls_back() -> None:
    themes, degraded = propose_themes(
        {"affinities": {"Drama": 0.7}}, _MARCH, llm=FakeLLMProvider(completion="not json")
    )
    assert any(t.include_genres == ["Drama"] for t in themes)
    assert degraded is True  # a configured LLM that failed → flagged as reduced mode


def _service(session: Session) -> RecommendationService:
    return RecommendationService(
        session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=None,
    )


def _seeded(session: Session) -> uuid.UUID:
    profile = Profile(display_name="me")
    session.add(profile)
    session.flush()
    seed_sample_data(session, profile.id)
    seed_sample_catalog(session)
    session.flush()
    return profile.id


def test_dynamic_rows_built_and_genre_scoped(db_session: Session) -> None:
    profile_id = _seeded(db_session)
    rows, _ = dynamic_rows(_service(db_session), profile_id, llm=None, now=_OCTOBER)

    assert rows
    spooky = next((r for r in rows if "Horror" in r.title or r.key.startswith("dyn:spooky")), None)
    assert spooky is not None
    # The spooky row is genre-scoped to horror candidates (catalog has horror titles).
    assert all("Horror" in item.genres for item in spooky.items)


def test_dynamic_rows_dedup_titles_across_themes(db_session: Session) -> None:
    # The themed rows draw from one taste centroid, so a broadly-appealing title (and the wildcard
    # discovery row especially) would otherwise surface in several rows — three evocatively-named
    # rows reading as one row relabelled. Dedup keeps each title in only its first/strongest theme.
    profile_id = _seeded(db_session)
    # The October fallback gives distinct themes (spooky horror, a taste-genre row, a wildcard
    # discovery row) whose candidate pools overlap — a realistic setup for cross-row collisions.
    rows, _ = dynamic_rows(_service(db_session), profile_id, llm=None, now=_OCTOBER)

    assert len(rows) >= 2  # more than one themed row survived
    ids = [item.title_id for row in rows for item in row.items]
    assert len(ids) == len(set(ids))  # and no title appears in two themed rows


def test_dynamic_rows_explanation_calls_are_bounded(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Like the static home rows, the themed rows must pool one bounded explanation budget rather
    # than fan out a per-item LLM call per card across every theme.
    monkeypatch.setattr("phare.recommend.service._EXPLANATION_CACHE", TTLCache(ttl=300))
    profile_id = _seeded(db_session)
    llm = FakeLLMProvider(completion="A great fit for your taste.")
    service = RecommendationService(
        db_session,
        embed_provider=LocalHashEmbeddingProvider(),
        embed_model_version=LOCAL_MODEL_VERSION,
        chat_llm=llm,
        explanation_budget=3,
    )

    rows, _ = dynamic_rows(service, profile_id, llm=None, now=_OCTOBER)  # deterministic themes

    assert sum(len(r.items) for r in rows) > 3  # many cards across themed rows...
    assert len(llm.prompts) <= 3  # ...but the LLM is called at most `budget` times, not per card


def test_dynamic_rows_long_llm_theme_logs_fitting_key(db_session: Session) -> None:
    # A realistic long theme title must not overflow recommendation_log.row_key (regression).
    long_title = (
        "Late-October horror you'd actually tolerate on a quiet school night with the lights on"
    )
    llm = FakeLLMProvider(completion=f'[{{"title":{long_title!r},"genres":["Horror"]}}]')
    profile_id = _seeded(db_session)

    rows, _ = dynamic_rows(_service(db_session), profile_id, llm=llm, now=_OCTOBER)

    assert rows
    keys = db_session.scalars(
        select(RecommendationLog.row_key).where(RecommendationLog.profile_id == profile_id)
    ).all()
    assert keys
    assert all(len(key) <= ROW_KEY_MAX_LEN for key in keys)


def test_dynamic_endpoint(db_session: Session) -> None:
    user = make_account(db_session)
    overrides = {
        get_embedder: lambda: Embedder(
            provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
        ),
        get_optional_chat_llm: lambda: None,
    }
    client = authed_client(db_session, user, overrides=overrides)

    profile_id = str(user.profile.id)
    client.post(f"/profiles/{profile_id}/sample-data")
    client.post("/catalog/sample")

    rows = client.get(f"/profiles/{profile_id}/recommendations/dynamic").json()["rows"]
    assert rows
    assert all(row["items"] for row in rows)
    assert all(item["explanation"] for row in rows for item in row["items"])
