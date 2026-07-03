"""phare_user.token_version: revocable bearer tokens

Tokens were stateless and un-revocable for their full 30-day TTL — no logout, no way to cut a
leaked token (review I3). This counter is folded into each token's signature; bumping it (logout,
password change, admin reset) invalidates every token issued before.

Deploying this invalidates all existing tokens once (everyone re-authenticates), since the signed
payload gains a segment. That's intentional — see docs/auth.md.

MERGE NOTE: this branch (Lot 7) forked from main at head 0018, so it chains off 0018. Lot 6 adds a
sibling 0019 off the same 0018. Whichever lot merges SECOND must rebase its migration's
``down_revision`` onto the first's head (or run ``alembic merge``) to avoid two heads.

Revision ID: 0020
Revises: 0018
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "phare_user",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("phare_user", "token_version")
