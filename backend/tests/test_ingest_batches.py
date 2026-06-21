"""Batched ingest: events commit progressively, and survive a mid-sync failure."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.api.sync import ingest_in_batches
from phare.db.models import EventType, TitleKind, WatchEvent
from phare.providers.fakes import FakeMetadataProvider
from phare.providers.types import RawEvent, RawMediaType, TitleMetadata
from tests.conftest import make_account


def _movie_meta(tmdb_id: int) -> TitleMetadata:
    return TitleMetadata(kind=TitleKind.movie, tmdb_id=tmdb_id, title=f"Movie {tmdb_id}", year=2020)


def _event(i: int) -> RawEvent:
    return RawEvent(
        source="trakt",
        media_type=RawMediaType.movie,
        type=EventType.watched,
        tmdb_id=1000 + i,
        external_ref=f"trakt:{i}",
    )


def _metadata(n: int) -> FakeMetadataProvider:
    return FakeMetadataProvider(
        titles={(1000 + i, TitleKind.movie): _movie_meta(1000 + i) for i in range(n)}
    )


def test_ingest_commits_every_batch(db_session: Session) -> None:
    user = make_account(db_session)
    events = [_event(i) for i in range(25)]
    result = ingest_in_batches(db_session, user.profile.id, _metadata(25), events, batch_size=10)
    assert result.created == 25
    count = db_session.scalar(
        select(func.count()).select_from(WatchEvent).where(WatchEvent.profile_id == user.profile.id)
    )
    assert count == 25


def test_partial_progress_survives_a_mid_sync_failure(db_session: Session) -> None:
    user = make_account(db_session)

    def exploding_stream() -> Iterator[RawEvent]:
        for i in range(10):
            yield _event(i)
        raise RuntimeError("source blew up mid-sync")

    # batch_size=5: the first two batches (10 events) commit before the stream raises.
    with pytest.raises(RuntimeError):
        ingest_in_batches(
            db_session, user.profile.id, _metadata(10), exploding_stream(), batch_size=5
        )

    # The committed batches persist — nothing rolled back — so a re-sync just fills in the rest.
    count = db_session.scalar(
        select(func.count()).select_from(WatchEvent).where(WatchEvent.profile_id == user.profile.id)
    )
    assert count == 10
