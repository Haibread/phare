"""incremental sync state

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("profile_id", "source", name="uq_sync_state"),
    )


def downgrade() -> None:
    op.drop_table("sync_state")
