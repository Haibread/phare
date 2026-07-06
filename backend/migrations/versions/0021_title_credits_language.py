"""title credits + original language: directors, top_cast, original_language

Round 8 (richer metadata). The broad TMDB *discover* seed carries only shallow records, so the
catalog has no cast/crew and — historically — dropped ``original_language`` on import. These three
columns let the detail sheet show who made a title and in what language it was produced, and are the
groundwork for true anime handling (Animation + Japanese origin) once ``original_language`` coverage
is backfilled.

``directors`` / ``top_cast`` are plain-name arrays (a director/creator list and the first ~5 billed
cast), defaulted to empty so existing rows are valid without a data migration. ``original_language``
is a nullable ISO-639-1 code, filled by import/heal, never guessed.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "title",
        sa.Column(
            "directors",
            ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "title",
        sa.Column(
            "top_cast",
            ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column("title", sa.Column("original_language", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("title", "original_language")
    op.drop_column("title", "top_cast")
    op.drop_column("title", "directors")
