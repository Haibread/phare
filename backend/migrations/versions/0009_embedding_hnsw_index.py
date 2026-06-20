"""HNSW ANN index on title_embedding.embedding

Candidate generation orders by cosine distance to the taste centroid on every recommendation;
without an ANN index that's a sequential scan + sort over the whole catalog. HNSW (cosine ops)
turns it into an index scan.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_title_embedding_hnsw",
        "title_embedding",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_title_embedding_hnsw", table_name="title_embedding")
