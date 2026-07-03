"""title_localization: cache a title's localized synopsis/genres per language

Opening a detail sheet fetched the localized synopsis + genres from TMDB live on every open
(~6 s observed, review C2). This table caches them by (title, language) with a long TTL, and
doubles as the fallback served when TMDB is unreachable.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "title_localization",
        sa.Column(
            "title_id",
            sa.Uuid(),
            sa.ForeignKey("title.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("language", sa.String(length=8), primary_key=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("genres", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("title_localization")
