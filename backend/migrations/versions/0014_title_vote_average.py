"""title vote_average: store TMDB mean rating as a re-ranker quality floor

The re-ranker penalises titles rated below a floor so a well-known but poorly-rated pick can't lead
a slate on popularity alone (review A1). This adds the column; it's backfilled by the next catalog
import/refresh (upsert refreshes it) — no data backfill here, the signal fills in on its own as the
app is used. See recommend/reranker.py.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("title", sa.Column("vote_average", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("title", "vote_average")
