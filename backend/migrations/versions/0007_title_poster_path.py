"""title poster_path

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("title", sa.Column("poster_path", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("title", "poster_path")
