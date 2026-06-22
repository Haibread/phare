"""title vote_count: store TMDB rating count to tier chat recommendations

The chat recommender mixes well-known and lesser-known picks by vote count, and orders the slate by
it. We already filtered/sorted TMDB by vote_count on import but never stored it; this adds the
column. Backfilled by the next catalog import (upsert refreshes it). See docs/agent.md.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("title", sa.Column("vote_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("title", "vote_count")
