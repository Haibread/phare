"""Sample data for trying the app with zero external setup (dev only).

Seeds a small, realistic history through the real ingestion pipeline, so the UI has
something to show without TMDB/Trakt credentials.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from phare.db.models import TitleKind
from phare.ingest.service import IngestionService, IngestResult
from phare.providers.types import RawEvent, RawMediaType, TitleMetadata

_SAMPLE_METADATA: dict[int, TitleMetadata] = {
    438631: TitleMetadata(
        kind=TitleKind.movie,
        tmdb_id=438631,
        title="Dune",
        year=2021,
        genres=["Science Fiction", "Adventure"],
        keywords=["desert", "spice", "messiah"],
        overview="A noble family becomes embroiled in a war for control of the planet Arrakis.",
    ),
    68718: TitleMetadata(
        kind=TitleKind.movie,
        tmdb_id=68718,
        title="Django Unchained",
        year=2012,
        genres=["Western", "Drama"],
        keywords=["bounty hunter", "slavery", "revenge"],
        overview="A freed slave sets out to rescue his wife from a brutal plantation owner.",
    ),
    95396: TitleMetadata(
        kind=TitleKind.show,
        tmdb_id=95396,
        title="Severance",
        year=2022,
        genres=["Drama", "Mystery", "Science Fiction"],
        keywords=["dystopia", "memory", "workplace"],
        overview="Employees surgically divide their memories between work and personal lives.",
    ),
}

_SAMPLE_EVENTS: list[RawEvent] = [
    RawEvent(
        source="sample",
        media_type=RawMediaType.movie,
        type="watched",
        tmdb_id=438631,
        occurred_at=datetime(2024, 11, 2, 20, 0, tzinfo=UTC),
        external_ref="sample:1",
    ),
    RawEvent(
        source="sample",
        media_type=RawMediaType.movie,
        type="rated",
        tmdb_id=438631,
        rating=9,
        occurred_at=datetime(2024, 11, 2, 22, 30, tzinfo=UTC),
        external_ref="sample:2",
    ),
    RawEvent(
        source="sample",
        media_type=RawMediaType.movie,
        type="rated",
        tmdb_id=68718,
        rating=8,
        occurred_at=datetime(2024, 9, 15, 21, 0, tzinfo=UTC),
        external_ref="sample:3",
    ),
    RawEvent(
        source="sample",
        media_type=RawMediaType.episode,
        type="watched",
        tmdb_id=95396,
        season_number=1,
        episode_number=1,
        occurred_at=datetime(2025, 1, 10, 19, 0, tzinfo=UTC),
        external_ref="sample:4",
    ),
    RawEvent(
        source="sample",
        media_type=RawMediaType.episode,
        type="watched",
        tmdb_id=95396,
        season_number=1,
        episode_number=2,
        occurred_at=datetime(2025, 1, 11, 19, 0, tzinfo=UTC),
        external_ref="sample:5",
    ),
    RawEvent(
        source="sample",
        media_type=RawMediaType.show,
        type="rated",
        tmdb_id=95396,
        rating=10,
        occurred_at=datetime(2025, 1, 12, 9, 0, tzinfo=UTC),
        external_ref="sample:6",
    ),
]


class _StaticMetadataProvider:
    """In-memory MetadataProvider over the sample dataset."""

    name = "sample"

    def get_title(self, tmdb_id: int, kind: TitleKind) -> TitleMetadata | None:
        return _SAMPLE_METADATA.get(tmdb_id)

    def find_by_imdb(self, imdb_id: str) -> None:
        return None


def seed_sample_data(session: Session, profile_id: uuid.UUID) -> IngestResult:
    """Ingest the sample history for a profile (idempotent via the ingestion pipeline)."""
    service = IngestionService(session, _StaticMetadataProvider())
    return service.ingest(profile_id, _SAMPLE_EVENTS)
