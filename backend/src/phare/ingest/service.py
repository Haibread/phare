"""Ingestion: resolve raw events to the canonical model and persist them.

Pipeline per event: resolve external ids -> canonical Title (+ Season/Episode for TV),
then idempotent upsert of a WatchEvent keyed by (profile, source, external_ref). Re-running
a sync is safe. Conflicting values on a duplicate are resolved most-recent-wins.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from phare.db.models import Episode, Season, Title, TitleKind, WatchEvent
from phare.providers.types import MetadataProvider, RawEvent, RawMediaType

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    titles_created: int = 0


def prefer_incoming(existing: datetime | None, incoming: datetime | None) -> bool:
    """Most-recent-wins conflict rule. An unknown incoming time never overwrites."""
    if incoming is None:
        return False
    if existing is None:
        return True
    return incoming >= existing


def exclude_events(
    session: Session,
    profile_id: uuid.UUID,
    *,
    before: datetime | None = None,
    title_ids: list[uuid.UUID] | None = None,
) -> int:
    """Import cleanup: mark a profile's events excluded (ignored by taste modeling).

    Requires at least one filter so it can never blank out an entire history by accident.
    """
    if before is None and not title_ids:
        raise ValueError("exclude_events requires `before` and/or `title_ids`")
    stmt = update(WatchEvent).where(WatchEvent.profile_id == profile_id)
    if before is not None:
        stmt = stmt.where(WatchEvent.occurred_at < before)
    if title_ids:
        stmt = stmt.where(WatchEvent.title_id.in_(title_ids))
    result = session.execute(stmt.values(excluded=True))
    return result.rowcount


class IngestionService:
    """Resolves and persists a stream of RawEvents for one profile."""

    def __init__(self, session: Session, metadata: MetadataProvider) -> None:
        self.session = session
        self.metadata = metadata
        self._title_cache: dict[int, Title] = {}

    def ingest(self, profile_id: uuid.UUID, events: Iterable[RawEvent]) -> IngestResult:
        result = IngestResult()
        for event in events:
            self._ingest_one(profile_id, event, result)
        self.session.flush()
        logger.info(
            "ingest.done",
            extra={
                "profile_id": str(profile_id),
                "created_count": result.created,
                "updated_count": result.updated,
                "skipped_count": result.skipped,
            },
        )
        return result

    @staticmethod
    def _kind_for(media_type: RawMediaType) -> TitleKind:
        return TitleKind.movie if media_type is RawMediaType.movie else TitleKind.show

    def _resolve_tmdb(self, event: RawEvent) -> tuple[int, TitleKind] | None:
        kind = self._kind_for(event.media_type)
        if event.tmdb_id is not None:
            return event.tmdb_id, kind
        if event.imdb_id is not None:
            match = self.metadata.find_by_imdb(event.imdb_id)
            if match is not None:
                return match.tmdb_id, match.kind
        return None

    def _get_or_create_title(
        self, tmdb_id: int, kind: TitleKind, result: IngestResult
    ) -> Title | None:
        if cached := self._title_cache.get(tmdb_id):
            return cached
        existing = self.session.scalar(select(Title).where(Title.tmdb_id == tmdb_id))
        if existing is not None:
            self._title_cache[tmdb_id] = existing
            return existing
        meta = self.metadata.get_title(tmdb_id, kind)
        if meta is None:
            logger.debug("ingest.title_unresolved", extra={"tmdb_id": tmdb_id})
            return None
        title = Title(
            kind=meta.kind,
            tmdb_id=meta.tmdb_id,
            imdb_id=meta.imdb_id,
            title=meta.title,
            year=meta.year,
            runtime_minutes=meta.runtime_minutes,
            overview=meta.overview,
            poster_path=meta.poster_path,
            genres=meta.genres,
            keywords=meta.keywords,
            popularity=meta.popularity,
        )
        self.session.add(title)
        self.session.flush()
        result.titles_created += 1
        self._title_cache[tmdb_id] = title
        return title

    def _get_or_create_season(self, title: Title, number: int) -> Season:
        season = self.session.scalar(
            select(Season).where(Season.show_id == title.id, Season.season_number == number)
        )
        if season is None:
            season = Season(show_id=title.id, season_number=number)
            self.session.add(season)
            self.session.flush()
        return season

    def _get_or_create_episode(self, season: Season, number: int) -> Episode:
        episode = self.session.scalar(
            select(Episode).where(Episode.season_id == season.id, Episode.episode_number == number)
        )
        if episode is None:
            episode = Episode(season_id=season.id, episode_number=number)
            self.session.add(episode)
            self.session.flush()
        return episode

    def _ingest_one(self, profile_id: uuid.UUID, event: RawEvent, result: IngestResult) -> None:
        resolved = self._resolve_tmdb(event)
        if resolved is None:
            result.skipped += 1
            return
        tmdb_id, kind = resolved
        title = self._get_or_create_title(tmdb_id, kind, result)
        if title is None:
            result.skipped += 1
            return

        season_id: uuid.UUID | None = None
        episode_id: uuid.UUID | None = None
        if (
            event.media_type is RawMediaType.episode
            and event.season_number is not None
            and event.episode_number is not None
        ):
            season = self._get_or_create_season(title, event.season_number)
            episode = self._get_or_create_episode(season, event.episode_number)
            season_id, episode_id = season.id, episode.id

        if event.external_ref is not None:
            existing = self.session.scalar(
                select(WatchEvent).where(
                    WatchEvent.profile_id == profile_id,
                    WatchEvent.source == event.source,
                    WatchEvent.external_ref == event.external_ref,
                )
            )
            if existing is not None:
                if self._update_existing(existing, event):
                    result.updated += 1
                else:
                    result.skipped += 1
                return

        self.session.add(
            WatchEvent(
                profile_id=profile_id,
                title_id=title.id,
                season_id=season_id,
                episode_id=episode_id,
                type=event.type,
                rating=event.rating,
                value_text=event.value_text,
                occurred_at=event.occurred_at,
                source=event.source,
                external_ref=event.external_ref,
            )
        )
        result.created += 1

    @staticmethod
    def _update_existing(existing: WatchEvent, event: RawEvent) -> bool:
        """Overwrite a duplicate only when the incoming event is at least as recent."""
        if not prefer_incoming(existing.occurred_at, event.occurred_at):
            return False
        changed = False
        existing_rating = float(existing.rating) if existing.rating is not None else None
        if existing_rating != event.rating:
            existing.rating = event.rating
            changed = True
        if existing.value_text != event.value_text:
            existing.value_text = event.value_text
            changed = True
        if existing.occurred_at != event.occurred_at:
            existing.occurred_at = event.occurred_at
            changed = True
        return changed
