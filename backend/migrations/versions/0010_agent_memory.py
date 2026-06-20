"""agent memory: watch commitments + generalist memory notes

The chat agent gains a write path: commitments ("I'll watch X") it can follow up on, and
free-text memory notes (soft/temporal context that distils into taste). See docs/agent.md.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Each enum is used in exactly one create_table below, so Alembic creates the PG type there once.
# drop_table does NOT drop the type, so downgrade drops them explicitly.
_COMMITMENT_STATUS = sa.Enum("pending", "watched", "dropped", name="commitment_status")
_MEMORY_KIND = sa.Enum("preference", "context", "fact", name="memory_kind")


def upgrade() -> None:
    op.create_table(
        "watch_commitment",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "title_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("title.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", _COMMITMENT_STATUS, nullable=False, server_default="pending"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_watch_commitment_profile", "watch_commitment", ["profile_id", "status"])

    op.create_table(
        "memory_note",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", _MEMORY_KIND, nullable=False, server_default="fact"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="chat"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_memory_note_profile", "memory_note", ["profile_id", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_memory_note_profile", table_name="memory_note")
    op.drop_table("memory_note")
    op.drop_index("ix_watch_commitment_profile", table_name="watch_commitment")
    op.drop_table("watch_commitment")
    bind = op.get_bind()
    _MEMORY_KIND.drop(bind, checkfirst=True)
    _COMMITMENT_STATUS.drop(bind, checkfirst=True)
