"""phare_user.token_version: revocable bearer tokens

Tokens were stateless and un-revocable for their full 30-day TTL — no logout, no way to cut a
leaked token (review I3). This counter is folded into each token's signature; bumping it (logout,
password change, admin reset) invalidates every token issued before.

Deploying this invalidates all existing tokens once (everyone re-authenticates), since the signed
payload gains a segment. That's intentional — see docs/auth.md.

Chains off 0019 (Lot 6's taste ``summary_by_lang``). Both were authored off main's 0018 on
independent lot branches; 0020 was rebased onto 0019 at merge time to keep a single alembic head.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "phare_user",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("phare_user", "token_version")
