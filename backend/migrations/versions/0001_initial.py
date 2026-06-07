"""initial canonical schema: title tree, profile, watch_event

Revision ID: 0001
Revises:
Create Date: 2026-06-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

title_kind = postgresql.ENUM("movie", "show", name="title_kind")
event_type = postgresql.ENUM(
    "watched",
    "rated",
    "liked",
    "disliked",
    "abandoned",
    "rewatched",
    "watchlisted",
    name="event_type",
)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    title_kind.create(bind, checkfirst=True)
    event_type.create(bind, checkfirst=True)

    op.create_table(
        "profile",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "title",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "kind",
            postgresql.ENUM(name="title_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("tmdb_id", sa.Integer(), nullable=True, unique=True),
        sa.Column("imdb_id", sa.String(length=20), nullable=True, unique=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("runtime_minutes", sa.Integer(), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("genres", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("keywords", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("popularity", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "season",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "show_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("title.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.UniqueConstraint("show_id", "season_number", name="uq_season_number"),
    )

    op.create_table(
        "episode",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "season_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("season.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("episode_number", sa.Integer(), nullable=False),
        sa.Column("runtime_minutes", sa.Integer(), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.UniqueConstraint("season_id", "episode_number", name="uq_episode_number"),
    )

    op.create_table(
        "watch_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "title_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("title.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "season_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("season.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "episode_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("episode.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "type",
            postgresql.ENUM(name="event_type", create_type=False),
            nullable=False,
        ),
        sa.Column("rating", sa.Numeric(precision=4, scale=2), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("external_ref", sa.String(length=200), nullable=True),
        sa.Column(
            "excluded",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "profile_id", "source", "external_ref", name="uq_watch_event_source_ref"
        ),
    )
    op.create_index("ix_watch_event_profile_title", "watch_event", ["profile_id", "title_id"])


def downgrade() -> None:
    op.drop_index("ix_watch_event_profile_title", table_name="watch_event")
    op.drop_table("watch_event")
    op.drop_table("episode")
    op.drop_table("season")
    op.drop_table("title")
    op.drop_table("profile")
    event_type.drop(op.get_bind(), checkfirst=True)
    title_kind.drop(op.get_bind(), checkfirst=True)
