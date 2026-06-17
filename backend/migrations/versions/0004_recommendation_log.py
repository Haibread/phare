"""recommendation log

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recommendation_log",
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
        sa.Column("row_key", sa.String(length=50), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("is_swing", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column(
            "shown_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_recommendation_log_profile", "recommendation_log", ["profile_id", "shown_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_recommendation_log_profile", table_name="recommendation_log")
    op.drop_table("recommendation_log")
