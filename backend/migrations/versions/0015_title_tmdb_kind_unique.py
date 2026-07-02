"""title: make tmdb_id unique per kind, not globally

TMDB's movie and TV id namespaces are disjoint — id 1398 is both *Stalker* (movie) and *The
Sopranos* (show). The original column-level UNIQUE(tmdb_id) treated them as one title, so importing
the second silently reused the first row and watch events attached to the wrong title (review H3a).
Drop that constraint and add a composite UNIQUE(tmdb_id, kind).

Defensive dedup first: under the old global-unique constraint no (tmdb_id, kind) *pair* can be
duplicated, so the dedup below is a guard for hand-edited / legacy data. It keeps the oldest row of
each pair, repoints the child records that carry user history (watch events, seasons, rec log,
commitments), then deletes the losers — their embeddings and cached explanations cascade away and
regenerate lazily. `downgrade` restores the global unique and will fail if the catalog now holds a
movie and a show sharing a tmdb_id (which is exactly what this migration set out to allow).

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Child tables whose rows carry user history and must be *repointed* to the surviving title (rather
# than cascade-deleted with the loser). Embeddings / explanations are intentionally omitted — they
# regenerate, so letting them cascade away with the loser row is fine.
_HISTORY_FKS: tuple[tuple[str, str], ...] = (
    ("watch_event", "title_id"),
    ("season", "show_id"),
    ("recommendation_log", "title_id"),
    ("watch_commitment", "title_id"),
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "CREATE TEMP TABLE _title_dupes ON COMMIT DROP AS "
            "SELECT id AS loser, first_value(id) OVER "
            "(PARTITION BY tmdb_id, kind ORDER BY created_at, id) AS keeper "
            "FROM title WHERE tmdb_id IS NOT NULL"
        )
    )
    conn.execute(sa.text("DELETE FROM _title_dupes WHERE loser = keeper"))
    for table, column in _HISTORY_FKS:
        conn.execute(
            sa.text(
                f"UPDATE {table} t SET {column} = d.keeper "  # noqa: S608 - table names are literals
                f"FROM _title_dupes d WHERE t.{column} = d.loser"
            )
        )
    conn.execute(sa.text("DELETE FROM title WHERE id IN (SELECT loser FROM _title_dupes)"))

    # IF EXISTS: on a legacy DB that somehow *has* (tmdb_id, kind) dupes, the global-unique
    # constraint must already be gone (it's what would have prevented them), so a plain drop would
    # fail. On a normal 0014 DB the constraint is present and this drops it.
    op.execute("ALTER TABLE title DROP CONSTRAINT IF EXISTS title_tmdb_id_key")
    op.create_unique_constraint("uq_title_tmdb_kind", "title", ["tmdb_id", "kind"])


def downgrade() -> None:
    op.drop_constraint("uq_title_tmdb_kind", "title", type_="unique")
    op.create_unique_constraint("title_tmdb_id_key", "title", ["tmdb_id"])
