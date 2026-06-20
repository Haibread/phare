"""title explanation cache: persist lazy "why this" reasons across restarts

The lazy explanation endpoint generates one workhorse-model sentence per opened card, keyed by
(title, taste fingerprint). It was cached only in-process, so every restart re-spent it. This
table makes accepted reasons durable. See docs/configuration.md (Row explanations).

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "title_explanation",
        sa.Column(
            "title_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("title.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("taste_fingerprint", sa.String(length=16), primary_key=True),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("title_explanation")
