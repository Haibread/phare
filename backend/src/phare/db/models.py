"""Canonical ORM models: the title tree, profiles, and the normalized event stream.

These are domain/persistence models — they never cross the wire. API DTOs live in the
``api`` layer and are mapped explicitly. See ``docs/data-model.md``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phare.db.base import Base

# Embedding vector dimension, fixed by the schema. Changing it (e.g. a different embedding
# model) requires a migration + full re-embed; see docs/data-model.md.
EMBEDDING_DIM = 1536

# Max length of a RecommendationLog.row_key. Dynamic rows derive their key from an LLM-supplied
# theme title, so the slug must be truncated to fit (see recommend/dynamic._slug). Single source
# of truth for both the column width and the slug cap; widening it needs a migration.
ROW_KEY_MAX_LEN = 120


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
    # Rejected from a card without watching ("not interested") — a negative taste signal that also
    # keeps the title out of future recs. Distinct from `disliked`, which implies it was watched.
    not_interested = "not_interested"


class CommitmentStatus(enum.StrEnum):
    """Lifecycle of a "I'll watch this" commitment the chat agent tracks."""

    pending = "pending"
    watched = "watched"
    dropped = "dropped"


class MemoryKind(enum.StrEnum):
    """Generalist agent memory note. ``preference`` is durable and distils into taste;
    ``context`` is usually temporal (carries ``expires_at``); ``fact`` is neutral recall."""

    preference = "preference"
    context = "context"
    fact = "fact"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Title(Base):
    """A canonical movie or show, keyed by external ids (TMDB primary, IMDb secondary)."""

    __tablename__ = "title"
    # TMDB's movie and TV id namespaces are disjoint — id 1398 is both *Stalker* (movie) and *The
    # Sopranos* (show) — so tmdb_id is unique only *per kind*, never globally. A column-level
    # UNIQUE(tmdb_id) collapsed such pairs onto one row and mis-attached events (review H3a).
    __table_args__ = (UniqueConstraint("tmdb_id", "kind", name="uq_title_tmdb_kind"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[TitleKind] = mapped_column(Enum(TitleKind, name="title_kind"))
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    # IMDb ids ARE globally unique across movies and shows, so this stays column-level unique.
    imdb_id: Mapped[str | None] = mapped_column(String(20), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    year: Mapped[int | None] = mapped_column(Integer)
    runtime_minutes: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str | None] = mapped_column(Text)
    poster_path: Mapped[str | None] = mapped_column(String(255))
    genres: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    popularity: Mapped[float | None] = mapped_column(Float)
    # TMDB rating count — proxy for how well-known a title is; used to tier chat recommendations.
    vote_count: Mapped[int | None] = mapped_column(Integer)
    # TMDB mean rating in [0, 10] — a crude quality floor the re-ranker penalises below (a
    # well-known but poorly-rated title shouldn't lead a slate). Distinct from vote_count (how
    # *many* rated, not how *well*). Nullable: filled by import/refresh, never guessed.
    vote_average: Mapped[float | None] = mapped_column(Float)
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


class User(Base):
    """A login identity — one human, one account, 1:1 with a :class:`Profile`.

    Credentials live in :class:`Identity` (a user may prove who they are via several providers:
    a local password, Plex, …). ``email`` is preferred metadata, filled when a provider supplies
    one (Plex does; Trakt may not) — it is *never* the identity key. See ``docs/auth.md``.
    """

    __tablename__ = "phare_user"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Nullable + unique: not every provider hands us an email, but when present it must be unique.
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    profile: Mapped[Profile] = relationship(back_populates="user", cascade="all, delete-orphan")
    identities: Mapped[list[Identity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Identity(Base):
    """One way a :class:`User` can authenticate. Keyed on ``(provider, subject)``.

    ``provider`` is a free string ("local", "plex", "trakt", …) so new providers add rows, never
    schema. ``subject`` is the provider's stable id for the user (email for local; Plex account id
    for plex). ``secret`` holds the argon2id password hash for ``local`` and is ``NULL`` for source
    providers (the proof is the OAuth grant; the source access token lives in ``SourceToken``).
    """

    __tablename__ = "identity"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("phare_user.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(50))
    subject: Mapped[str] = mapped_column(String(320))
    secret: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="identities")


class PlexServerBinding(Base):
    """A Plex server the owner can access — the membership gate for "Sign in with Plex".

    Set at the owner's first sign-in (one row per accessible server machine identifier). A later
    Plex sign-in is allowed iff the signing-in account shares at least one of these servers. See
    ``docs/auth.md``.
    """

    __tablename__ = "plex_server_binding"

    machine_identifier: Mapped[str] = mapped_column(String(100), primary_key=True)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Profile(Base):
    """The taste/history container for one human — 1:1 with its :class:`User`."""

    __tablename__ = "profile"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Unique + nullable: every account-created profile has an owner; legacy rows (pre-multi-user)
    # may be NULL and are simply unreachable until claimed. See ``docs/auth.md``.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("phare_user.id", ondelete="CASCADE"), unique=True
    )
    display_name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User | None] = relationship(back_populates="profile")


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


class TitleEmbedding(Base):
    """Content embedding for a title, stamped with the embedding model version.

    Embeddings are at title (movie/show) level; never mix model versions in one space.
    """

    __tablename__ = "title_embedding"

    # HNSW ANN index for cosine similarity — candidate generation orders by `embedding <=> centroid`
    # on every recommendation, so without this it's a sequential scan + sort over the whole catalog.
    # Defined here (not only in the migration) so create_all builds it for tests too.
    __table_args__ = (
        Index(
            "ix_title_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    # Composite PK so multiple model versions can coexist (clean cutover during a re-embed).
    title_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("title.id", ondelete="CASCADE"), primary_key=True
    )
    model_version: Mapped[str] = mapped_column(String(100), primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TitleExplanation(Base):
    """A cached "why this fits you" reason for a (title, taste version) pair.

    The lazy explanation endpoint generates one workhorse-model sentence per card the user opens.
    Keying it by the title and the taste *fingerprint* (a hash of the taste summary) lets it
    persist across restarts and replicas — so a reason is generated once per taste version, not
    re-spent every time the process recycles. Self-invalidates when taste changes (new fingerprint
    → new row); stale rows are harmless and just age out of relevance. See ``recommend/explain.py``.
    """

    __tablename__ = "title_explanation"

    title_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("title.id", ondelete="CASCADE"), primary_key=True
    )
    # The taste fingerprint: sha256(summary)[:16], so a fixed-width 16-char hex string.
    taste_fingerprint: Mapped[str] = mapped_column(String(16), primary_key=True)
    explanation: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TitleLocalization(Base):
    """Cached localized synopsis + genres for a (title, language), fetched from TMDB.

    A pure metadata cache — no LLM. The detail view shows the synopsis/genres in the request
    language; without this it hit TMDB live on every open (~6 s observed, review C2). Keyed by
    title + language, refreshed once past a long TTL, and served as the fallback when TMDB is
    unreachable. ``fetched_at`` is the write time the TTL is measured against.
    """

    __tablename__ = "title_localization"

    title_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("title.id", ondelete="CASCADE"), primary_key=True
    )
    language: Mapped[str] = mapped_column(String(8), primary_key=True)
    overview: Mapped[str | None] = mapped_column(Text)
    genres: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceToken(Base):
    """A per-profile access token for an external source (Trakt/Plex/Jellyfin), encrypted.

    The plaintext is never stored; ``token_encrypted`` is Fernet ciphertext derived from
    ``SECRET_KEY``. One token per (profile, source).
    """

    __tablename__ = "source_token"
    __table_args__ = (UniqueConstraint("profile_id", "source", name="uq_source_token"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(50))
    token_encrypted: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SyncState(Base):
    """High-water mark for incremental source syncs — when we last pulled (profile, source).

    Lets a re-sync ask the source only for events since this timestamp instead of the whole
    history. Re-ingesting is idempotent regardless; this is purely an API-quota optimisation.
    """

    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("profile_id", "source", name="uq_sync_state"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(50))
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RecommendationLog(Base):
    """Every recommendation shown to a profile (a row item or a chat suggestion).

    Closed-loop groundwork: pairing these with later watch events is how we'll measure whether
    recommendations actually land. Per-profile and FK-cascaded — no cross-user data.
    """

    __tablename__ = "recommendation_log"
    __table_args__ = (Index("ix_recommendation_log_profile", "profile_id", "shown_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"))
    title_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    row_key: Mapped[str] = mapped_column(String(ROW_KEY_MAX_LEN))
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(Float)
    is_swing: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    source: Mapped[str] = mapped_column(String(20))  # "row" | "chat"
    shown_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TasteProfile(Base):
    """The LLM-extracted, user-editable taste profile — one per profile.

    ``structured`` is the generated model; ``user_overrides`` are sticky hand edits that win
    over generation. The effective profile is structured with overrides applied on top.
    """

    __tablename__ = "taste_profile"

    id: Mapped[uuid.UUID] = _uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profile.id", ondelete="CASCADE"), unique=True
    )
    model_version: Mapped[str | None] = mapped_column(String(100))
    summary_text: Mapped[str | None] = mapped_column(Text)
    # ``summary_text`` in the language it was generated in; ``summary_by_lang`` caches on-demand
    # translations keyed by language so the profile reads in the UI's language, not the ingestion
    # language (review F1). Reset on regeneration; each language costs one workhorse call once.
    summary_by_lang: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    structured: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    user_overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    confidence: Mapped[float | None] = mapped_column(Float)
    # True when this profile came from the deterministic (genre-frequency) fallback because the LLM
    # extraction failed — a provider blip shouldn't freeze a coarse profile forever, so the auto
    # refresh re-attempts extraction while this is set (review A14).
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WatchCommitment(Base):
    """A "I'll watch X" intent the chat agent registers, so it can follow up next session.

    Resolving it (watched/dropped) is what turns a plan into a real signal. Per-profile and
    FK-cascaded; inspectable in the UI (memory is never a black box).
    """

    __tablename__ = "watch_commitment"
    __table_args__ = (Index("ix_watch_commitment_profile", "profile_id", "status"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"))
    title_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("title.id", ondelete="CASCADE"))
    status: Mapped[CommitmentStatus] = mapped_column(
        Enum(CommitmentStatus, name="commitment_status"),
        default=CommitmentStatus.pending,
        server_default=CommitmentStatus.pending.value,
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryNote(Base):
    """Generalist, free-text agent memory — the soft/contextual/temporal facts no schema fits.

    A steering input, never a ranking authority: durable preferences distil into the taste
    profile; temporal notes (``expires_at`` set) only colour the active session. Editable in the
    UI like the taste profile.
    """

    __tablename__ = "memory_note"
    __table_args__ = (Index("ix_memory_note_profile", "profile_id", "expires_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[MemoryKind] = mapped_column(
        Enum(MemoryKind, name="memory_kind"),
        default=MemoryKind.fact,
        server_default=MemoryKind.fact.value,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(50), default="chat", server_default="chat")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
