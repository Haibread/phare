"""taste_profile.degraded: mark a profile built from the deterministic fallback

When the LLM taste extraction fails, Phare falls back to a genre-frequency profile so the profile is
never blank. But that coarse profile used to be treated as final, so a transient provider outage
froze personalisation until enough new events accrued (review A14). This flag lets the auto refresh
re-attempt extraction while it's set; it clears on the next successful extraction.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "taste_profile",
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("taste_profile", "degraded")
