"""Closed-loop conversion: the recommendation_log x watch_event join, with explicit timing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.api.deps import Embedder, get_embedder, get_optional_chat_llm
from phare.db.base import get_session
from phare.db.models import (
    EventType,
    Profile,
    RecommendationLog,
    Title,
    TitleKind,
    WatchEvent,
)
from phare.eval.conversion import ConversionStats, conversion_stats, format_conversion
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider

_NOW = datetime(2026, 6, 1, tzinfo=UTC)
_SHOWN = _NOW - timedelta(days=30)  # mature: window has long since elapsed


def _profile(session: Session) -> uuid.UUID:
    profile = Profile(display_name="me")
    session.add(profile)
    session.flush()
    return profile.id


_tmdb_seq = iter(range(900000, 999999))


def _title(session: Session) -> uuid.UUID:
    title = Title(kind=TitleKind.movie, tmdb_id=next(_tmdb_seq), title="T")
    session.add(title)
    session.flush()
    return title.id


def _log(
    session: Session,
    profile_id: uuid.UUID,
    title_id: uuid.UUID,
    *,
    shown_at: datetime = _SHOWN,
    rank: int = 0,
    is_swing: bool = False,
) -> None:
    session.add(
        RecommendationLog(
            profile_id=profile_id,
            title_id=title_id,
            row_key="you_might_like",
            rank=rank,
            score=1.0,
            is_swing=is_swing,
            source="row",
            shown_at=shown_at,
        )
    )
    session.flush()


def _watch(
    session: Session,
    profile_id: uuid.UUID,
    title_id: uuid.UUID,
    *,
    occurred_at: datetime,
    event_type: EventType = EventType.watched,
) -> None:
    session.add(
        WatchEvent(
            profile_id=profile_id,
            title_id=title_id,
            type=event_type,
            occurred_at=occurred_at,
            source="trakt",
        )
    )
    session.flush()


def _stats(session: Session, profile_id: uuid.UUID, **kw):
    return conversion_stats(session, profile_id=profile_id, now=_NOW, **kw)


def test_watch_within_window_converts(db_session: Session) -> None:
    p, t = _profile(db_session), _title(db_session)
    _log(db_session, p, t)
    _watch(db_session, p, t, occurred_at=_SHOWN + timedelta(days=5))

    stats = _stats(db_session, p)
    assert (stats.shown, stats.converted, stats.rate) == (1, 1, 1.0)


def test_watch_outside_window_or_before_does_not_convert(db_session: Session) -> None:
    p = _profile(db_session)
    late, before = _title(db_session), _title(db_session)
    _log(db_session, p, late)
    _log(db_session, p, before)
    _watch(db_session, p, late, occurred_at=_SHOWN + timedelta(days=20))  # past the 14-day window
    _watch(db_session, p, before, occurred_at=_SHOWN - timedelta(days=1))  # watched before shown

    stats = _stats(db_session, p)
    assert (stats.shown, stats.converted) == (2, 0)


def test_rank_beyond_top_k_excluded(db_session: Session) -> None:
    p, t = _profile(db_session), _title(db_session)
    _log(db_session, p, t, rank=20)
    _watch(db_session, p, t, occurred_at=_SHOWN + timedelta(days=1))
    assert _stats(db_session, p, top_k=10).shown == 0


def test_swings_are_reported_separately(db_session: Session) -> None:
    p = _profile(db_session)
    main, swing = _title(db_session), _title(db_session)
    _log(db_session, p, main, is_swing=False)
    _log(db_session, p, swing, is_swing=True)
    _watch(db_session, p, swing, occurred_at=_SHOWN + timedelta(days=2))

    stats = _stats(db_session, p)
    assert (stats.shown, stats.converted) == (1, 0)  # swing not in the accuracy rate
    assert (stats.swing_shown, stats.swing_converted) == (1, 1)


def test_immature_impressions_excluded(db_session: Session) -> None:
    p, t = _profile(db_session), _title(db_session)
    _log(db_session, p, t, shown_at=_NOW - timedelta(days=2))  # window not yet elapsed
    _watch(db_session, p, t, occurred_at=_NOW - timedelta(days=1))
    assert _stats(db_session, p, within_days=14).shown == 0
    # …but visible when maturity is not required.
    assert (
        conversion_stats(
            db_session, profile_id=p, within_days=14, require_mature=False, now=_NOW
        ).shown
        == 1
    )


def test_conversion_is_per_profile(db_session: Session) -> None:
    a, b = _profile(db_session), _profile(db_session)
    t = _title(db_session)
    _log(db_session, a, t)
    _watch(db_session, b, t, occurred_at=_SHOWN + timedelta(days=1))  # B watched, not A
    assert _stats(db_session, a).converted == 0


def test_format_conversion_lines() -> None:
    stats = ConversionStats(
        shown=8,
        converted=2,
        rate=0.25,
        swing_shown=3,
        swing_converted=0,
        swing_rate=0.0,
        top_k=10,
        within_days=14,
    )
    lines = format_conversion(stats, "all profiles")
    assert lines[0] == "Conversion (all profiles, top-10, 14d): 25.0% (2/8 matured impressions)"
    assert "swings" in lines[1]


def test_format_conversion_handles_no_data() -> None:
    stats = ConversionStats(
        shown=0,
        converted=0,
        rate=None,
        swing_shown=0,
        swing_converted=0,
        swing_rate=None,
        top_k=10,
        within_days=14,
    )
    assert "n/a" in format_conversion(stats, "profile x")[0]


# --- API --------------------------------------------------------------------


def _client(session: Session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_embedder] = lambda: Embedder(
        provider=LocalHashEmbeddingProvider(), model_version=LOCAL_MODEL_VERSION
    )
    app.dependency_overrides[get_optional_chat_llm] = lambda: None
    return TestClient(app)


def test_conversion_endpoint_shape(db_session: Session) -> None:
    p, t = _profile(db_session), _title(db_session)
    _log(db_session, p, t)
    _watch(db_session, p, t, occurred_at=_SHOWN + timedelta(days=3))

    body = (
        _client(db_session)
        .get(f"/profiles/{p}/recommendations/conversion?topK=10&withinDays=14")
        .json()
    )
    assert body["topK"] == 10
    assert body["withinDays"] == 14
    assert body["shown"] >= 1
    assert "rate" in body and "swingRate" in body
