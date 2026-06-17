"""source token storage

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_token",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("token_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("profile_id", "source", name="uq_source_token"),
    )


def downgrade() -> None:
    op.drop_table("source_token")
