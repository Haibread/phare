"""Canonical ORM models: the title tree, profiles, and the normalized event stream.

These are domain/persistence models — they never cross the wire. API DTOs live in the
``api`` layer and are mapped explicitly. See ``docs/data-model.md``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phare.db.base import Base


class TitleKind(enum.StrEnum):
    movie = "movie"
    show = "show"


class EventType(enum.StrEnum):
    watched = "watched"
    rated = "rated"
    liked = "liked"
    disliked = "disliked"
    abandoned = "abandoned"
    rewatched = "rewatched"
    watchlisted = "watchlisted"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Title(Base):
    """A canonical movie or show, keyed by external ids (TMDB primary, IMDb secondary)."""

    __tablename__ = "title"

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[TitleKind] = mapped_column(Enum(TitleKind, name="title_kind"))
    tmdb_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    imdb_id: Mapped[str | None] = mapped_column(String(20), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    year: Mapped[int | None] = mapped_column(Integer)
    runtime_minutes: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str | None] = mapped_column(Text)
    genres: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    popularity: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    seasons: Mapped[list[Season]] = relationship(
        back_populates="show", cascade="all, delete-orphan"
    )


class Season(Base):
    __tablename__ = "season"
    __table_args__ = (UniqueConstraint("show_id", "season_number", name="uq_season_number"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    show_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    season_number: Mapped[int] = mapped_column(Integer)
    overview: Mapped[str | None] = mapped_column(Text)

    show: Mapped[Title] = relationship(back_populates="seasons")
    episodes: Mapped[list[Episode]] = relationship(
        back_populates="season", cascade="all, delete-orphan"
    )


class Episode(Base):
    __tablename__ = "episode"
    __table_args__ = (UniqueConstraint("season_id", "episode_number", name="uq_episode_number"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    season_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("season.id", ondelete="CASCADE"))
    episode_number: Mapped[int] = mapped_column(Integer)
    runtime_minutes: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str | None] = mapped_column(Text)

    season: Mapped[Season] = relationship(back_populates="episodes")


class Profile(Base):
    """One human. (One account = one user; auth is added later.)"""

    __tablename__ = "profile"

    id: Mapped[uuid.UUID] = _uuid_pk()
    display_name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WatchEvent(Base):
    """A normalized, source-agnostic event for one profile. See ``docs/data-model.md``."""

    __tablename__ = "watch_event"
    __table_args__ = (
        UniqueConstraint("profile_id", "source", "external_ref", name="uq_watch_event_source_ref"),
        Index("ix_watch_event_profile_title", "profile_id", "title_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"))
    title_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    season_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("season.id", ondelete="CASCADE"))
    episode_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("episode.id", ondelete="CASCADE")
    )
    type: Mapped[EventType] = mapped_column(Enum(EventType, name="event_type"))
    rating: Mapped[float | None] = mapped_column(Numeric(4, 2))
    value_text: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(50))
    external_ref: Mapped[str | None] = mapped_column(String(200))
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
