"""event_type: add 'not_interested' — the card-level "not interested" feedback signal

A negative taste signal a user can send from a recommendation card (review K2). It feeds the taste
pipeline like any other event and, being an event on the title, keeps that title out of future
candidate generation. Distinct from `disliked`, which implies the title was actually watched.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent so a re-run (or a DB already carrying the value) is a no-op. Adding a value to an
    # enum is fine inside Alembic's transaction, as long as the value isn't *used* in that same one.
    op.execute("ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'not_interested'")


def downgrade() -> None:
    # Postgres has no DROP VALUE for an enum; removing it would mean recreating the type and
    # rewriting every event_type column. The unused value is harmless, so downgrade is a no-op.
    pass
