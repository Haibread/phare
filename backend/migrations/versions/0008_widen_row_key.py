"""widen recommendation_log.row_key

Dynamic-row keys derive from LLM-supplied theme titles; 50 chars overflowed. Widen to 120
(see phare.db.models.ROW_KEY_MAX_LEN). The slug is also truncated in code, so this is headroom.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "recommendation_log",
        "row_key",
        existing_type=sa.String(length=50),
        type_=sa.String(length=120),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "recommendation_log",
        "row_key",
        existing_type=sa.String(length=120),
        type_=sa.String(length=50),
        existing_nullable=False,
    )
