"""taste_profile.summary_by_lang: cache the taste summary per UI language

The taste summary was generated once in the ingestion-time language and never re-localized, so a
French UI showed an English summary (review F1). This JSONB column caches on-demand translations
keyed by language, seeded with the native summary on each (re)generation, so each language costs at
most one workhorse call once.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "taste_profile",
        sa.Column(
            "summary_by_lang",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("taste_profile", "summary_by_lang")
