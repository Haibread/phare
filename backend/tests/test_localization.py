"""Localized display titles on cards (round 12 follow-up).

Persisted ``Title`` text is canonical (language-neutral), so a French user saw "Kara Sevda" on
every card where TMDB-fr says "Amour éternel". Cards now carry a ``displayTitle`` stamped in bulk
from the ``title_localization`` cache — one query per response, never a TMDB fetch on the hot
path — and the misses are filled by a background worker as titles get served.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

import phare.catalog.localization as loc
from phare.catalog.localization import (
    display_titles,
    localization_fill_running,
    run_localization_fill,
    schedule_localization_fill,
)
from phare.core.config import Settings
from phare.db.models import Title, TitleKind, TitleLocalization
from phare.providers.fakes import FakeMetadataProvider
from phare.providers.types import TitleMetadata
from tests.conftest import authed_client, make_account


@pytest.fixture(autouse=True)
def _reset_worker_state() -> None:
    yield
    with loc._lock:
        loc._pending.clear()
        loc._running = False


def _add_title(session: Session, *, name: str, tmdb_id: int) -> Title:
    title = Title(
        kind=TitleKind.movie,
        title=name,
        year=2015,
        tmdb_id=tmdb_id,
        genres=["Drama"],
        overview=f"Canonical overview of {name}.",
        runtime_minutes=100,
        original_language="tr",
    )
    session.add(title)
    session.flush()
    return title


def _localize(
    session: Session, title: Title, *, language: str = "fr", name: str | None = None
) -> TitleLocalization:
    row = TitleLocalization(
        title_id=title.id,
        language=language,
        title=name,
        overview=f"Synopsis localisé de {title.title}.",
        genres=["Drame"],
    )
    session.add(row)
    session.flush()
    return row


# --- display_titles: the hot-path bulk read -----------------------------------------------------


def test_display_titles_returns_cached_names_in_one_query(db_session: Session) -> None:
    # The whole point: a page of cards costs ONE localization SELECT, never N+1.
    titles = [_add_title(db_session, name=f"T{i}", tmdb_id=100 + i) for i in range(4)]
    for t in titles[:3]:
        _localize(db_session, t, name=f"{t.title} (fr)")

    statements: list[str] = []
    engine = db_session.get_bind().engine

    def _count(conn: object, cursor: object, statement: str, *args: object) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        names = display_titles(db_session, "fr", [t.id for t in titles])
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert names == {t.id: f"{t.title} (fr)" for t in titles[:3]}
    assert len(statements) == 1  # bulk: one SELECT for the whole id set


def test_display_titles_is_a_noop_for_the_canonical_language(db_session: Session) -> None:
    title = _add_title(db_session, name="Kara Sevda", tmdb_id=200)
    _localize(db_session, title, language="en", name="Endless Love")
    assert display_titles(db_session, "en", [title.id]) == {}


def test_display_titles_queues_misses_for_the_background_fill(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Missing localizations must not fetch inline (latency budget) — they queue for the worker.
    # A cached row WITHOUT a name (pre-column fill) counts as a miss too, so old rows self-heal.
    localized = _add_title(db_session, name="Kara Sevda", tmdb_id=300)
    _localize(db_session, localized, name="Amour éternel")
    unlocalized = _add_title(db_session, name="The Worst Witch", tmdb_id=301)
    nameless = _add_title(db_session, name="Dark", tmdb_id=302)
    _localize(db_session, nameless, name=None)

    scheduled: list[tuple[str, set[uuid.UUID]]] = []
    monkeypatch.setattr(
        loc,
        "schedule_localization_fill",
        lambda settings, language, ids, **_: scheduled.append((language, set(ids))) or True,
    )

    names = display_titles(db_session, "fr", [localized.id, unlocalized.id, nameless.id])

    assert names == {localized.id: "Amour éternel"}
    assert scheduled == [("fr", {unlocalized.id, nameless.id})]


def test_display_titles_schedules_nothing_when_everything_is_cached(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    title = _add_title(db_session, name="Kara Sevda", tmdb_id=310)
    _localize(db_session, title, name="Amour éternel")
    monkeypatch.setattr(
        loc,
        "schedule_localization_fill",
        lambda *a, **k: pytest.fail("no misses — nothing to schedule"),
    )
    assert display_titles(db_session, "fr", [title.id]) == {title.id: "Amour éternel"}


# --- run_localization_fill: the worker's synchronous unit ----------------------------------------


def test_run_fill_caches_localized_text_and_skips_already_localized(db_session: Session) -> None:
    fresh = _add_title(db_session, name="Kara Sevda", tmdb_id=400)
    done = _add_title(db_session, name="Dark", tmdb_id=401)
    _localize(db_session, done, name="Dark (fr)")
    provider = FakeMetadataProvider(
        titles={
            (400, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie,
                tmdb_id=400,
                title="Amour éternel",
                overview="Synopsis en français.",
                genres=["Drame"],
            )
        }
    )

    written = run_localization_fill(db_session, provider, "fr", [fresh.id, done.id])

    assert written == 1
    assert provider.calls == [(400, TitleKind.movie)]  # the localized one cost no fetch
    cached = db_session.get(TitleLocalization, {"title_id": fresh.id, "language": "fr"})
    assert cached is not None
    assert cached.title == "Amour éternel"
    assert cached.overview == "Synopsis en français."
    assert cached.genres == ["Drame"]


def test_run_fill_heals_a_nameless_cache_row(db_session: Session) -> None:
    # Rows cached before the `title` column existed: overview present, name NULL. The fill must
    # refetch and set the name (upsert, not insert-only).
    title = _add_title(db_session, name="Kara Sevda", tmdb_id=410)
    _localize(db_session, title, name=None)
    provider = FakeMetadataProvider(
        titles={
            (410, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie, tmdb_id=410, title="Amour éternel"
            )
        }
    )

    assert run_localization_fill(db_session, provider, "fr", [title.id]) == 1
    cached = db_session.get(TitleLocalization, {"title_id": title.id, "language": "fr"})
    db_session.refresh(cached)
    assert cached.title == "Amour éternel"


def test_run_fill_skips_titles_without_a_tmdb_id(db_session: Session) -> None:
    orphan = _add_title(db_session, name="Local Only", tmdb_id=420)
    orphan.tmdb_id = None
    db_session.flush()
    provider = FakeMetadataProvider()
    assert run_localization_fill(db_session, provider, "fr", [orphan.id]) == 0
    assert provider.calls == []


# --- schedule_localization_fill: the self-triggering worker --------------------------------------


def test_schedule_is_a_noop_offline(db_session: Session) -> None:
    # No TMDB key → nothing to fetch from; must not mark the worker running (principle 5).
    started = schedule_localization_fill(Settings(tmdb_api_key=None), "fr", [uuid.uuid4()])
    assert started is False
    assert localization_fill_running() is False


def test_schedule_runs_via_injected_runner_and_dedupes(db_session: Session) -> None:
    ran: list[bool] = []
    started = schedule_localization_fill(
        Settings(tmdb_api_key="x"), "fr", [uuid.uuid4()], runner=lambda work: ran.append(True)
    )
    assert started is True
    assert ran == [True]

    # While "running", later requests only queue their ids — no second worker.
    assert (
        schedule_localization_fill(
            Settings(tmdb_api_key="x"), "fr", [uuid.uuid4()], runner=lambda w: None
        )
        is False
    )
    assert len(loc._pending["fr"]) >= 1  # the second call's ids joined the queue


def test_schedule_bounds_the_pending_queue(db_session: Session) -> None:
    ids = [uuid.uuid4() for _ in range(loc._MAX_PENDING + 50)]
    schedule_localization_fill(Settings(tmdb_api_key="x"), "fr", ids, runner=lambda w: None)
    assert len(loc._pending["fr"]) <= loc._MAX_PENDING


def test_drain_processes_queued_batches_synchronously(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Drive the whole worker loop synchronously (mirrors conftest's _sync_embedding_backfill): the
    # queue drains through run_localization_fill on the test session, and the flag clears.
    title = _add_title(db_session, name="Kara Sevda", tmdb_id=500)
    provider = FakeMetadataProvider(
        titles={
            (500, TitleKind.movie): TitleMetadata(
                kind=TitleKind.movie, tmdb_id=500, title="Amour éternel"
            )
        }
    )

    def _sync_drain(settings: Settings) -> None:
        while (item := loc._pop_batch()) is not None:
            language, batch = item
            run_localization_fill(db_session, provider, language, batch)

    # The runner is swallowed (the real _drain would open its own DB connection); the test drains
    # the queue itself, synchronously, on the rolled-back session.
    started = schedule_localization_fill(
        Settings(tmdb_api_key="x"), "fr", [title.id], runner=lambda work: None
    )
    assert started is True
    _sync_drain(Settings(tmdb_api_key="x"))

    cached = db_session.get(TitleLocalization, {"title_id": title.id, "language": "fr"})
    assert cached is not None and cached.title == "Amour éternel"
    assert localization_fill_running() is False  # _pop_batch cleared the flag on empty


# --- the wire: displayTitle on card DTOs ----------------------------------------------------------


def _search(client: object, profile_id: uuid.UUID, query: str, language: str) -> list[dict]:
    resp = client.post(
        f"/profiles/{profile_id}/catalog/search",
        json={"q": query},
        headers={"Accept-Language": language},
    )
    assert resp.status_code == 200
    return resp.json()["results"]


def test_search_cards_carry_the_cached_display_title_for_french(db_session: Session) -> None:
    user = make_account(db_session)
    title = _add_title(db_session, name="Kara Sevda", tmdb_id=600)
    _localize(db_session, title, name="Amour éternel")
    client = authed_client(db_session, user)

    results = _search(client, user.profile.id, "Kara Sevda", "fr")

    (card,) = [r for r in results if r["titleId"] == str(title.id)]
    assert card["title"] == "Kara Sevda"  # canonical stays — additive, nothing breaks
    assert card["displayTitle"] == "Amour éternel"


def test_search_cards_send_null_display_title_for_english(db_session: Session) -> None:
    user = make_account(db_session)
    title = _add_title(db_session, name="Kara Sevda", tmdb_id=601)
    _localize(db_session, title, language="fr", name="Amour éternel")
    client = authed_client(db_session, user)

    results = _search(client, user.profile.id, "Kara Sevda", "en")

    (card,) = [r for r in results if r["titleId"] == str(title.id)]
    assert card["displayTitle"] is None


def test_uncached_titles_fall_back_to_canonical_without_any_fetch(db_session: Session) -> None:
    # No localization cached and no TMDB (hermetic) — the card must still serve, canonical.
    user = make_account(db_session)
    title = _add_title(db_session, name="The Worst Witch", tmdb_id=602)
    client = authed_client(db_session, user)

    results = _search(client, user.profile.id, "Worst Witch", "fr")

    (card,) = [r for r in results if r["titleId"] == str(title.id)]
    assert card["title"] == "The Worst Witch"
    assert card["displayTitle"] is None


# --- migration ↔ model ---------------------------------------------------------------------------


def test_localized_title_migration_exists_and_matches_the_model() -> None:
    path = (
        Path(__file__).parents[1] / "migrations" / "versions" / "0022_title_localization_title.py"
    )
    source = path.read_text()
    assert 'op.add_column("title_localization"' in source
    assert '"title"' in source and "nullable=True" in source
    # The ORM column the migration must mirror: nullable varchar on title_localization.
    column = TitleLocalization.__table__.c.title
    assert column.nullable is True
    assert column.type.length == 500
