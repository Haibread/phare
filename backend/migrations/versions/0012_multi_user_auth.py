"""multi-user auth: users, identities, plex server binding, profile ownership

Replaces the single shared-password gate with real per-user accounts. A ``phare_user`` is one
human (1:1 with a ``profile``); an ``identity`` is how they authenticate (local password, Plex,
…), keyed on ``(provider, subject)``. ``plex_server_binding`` records the owner's Plex servers,
the membership gate for "Sign in with Plex". Existing profiles get a nullable ``user_id`` and are
unreachable until claimed. See docs/auth.md.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "phare_user",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=True, unique=True),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_table(
        "identity",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("phare_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("subject", sa.String(length=320), nullable=False),
        sa.Column("secret", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),
    )
    op.create_table(
        "plex_server_binding",
        sa.Column("machine_identifier", sa.String(length=100), primary_key=True),
        sa.Column(
            "bound_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "profile",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_profile_user", "profile", "phare_user", ["user_id"], ["id"], ondelete="CASCADE"
    )
    op.create_unique_constraint("uq_profile_user", "profile", ["user_id"])


def downgrade() -> None:
    op.drop_constraint("uq_profile_user", "profile", type_="unique")
    op.drop_constraint("fk_profile_user", "profile", type_="foreignkey")
    op.drop_column("profile", "user_id")
    op.drop_table("plex_server_binding")
    op.drop_table("identity")
    op.drop_table("phare_user")
