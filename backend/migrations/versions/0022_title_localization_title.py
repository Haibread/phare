"""title_localization localized display name: title

Round 12 follow-up. Persisted ``Title`` text is canonical (language-neutral) so the shared
embedding space and the English genre vocabulary stay clean — but that left French users seeing
canonical names on every card. The ``title_localization`` cache already holds the per-language
synopsis/genres; this adds the localized display *name* ("Amour éternel" for *Kara Sevda*) so
cards can show it without touching TMDB on the hot path.

Nullable and additive: rows cached before this column have no localized name yet — the read path
treats them as missing and the lazy background fill (``catalog/localization.py``) heals them as
titles get served.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("title_localization", sa.Column("title", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("title_localization", "title")
