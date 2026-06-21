"""Embedding pipeline: input building, storage, versioning, idempotency."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.db.models import EMBEDDING_DIM, Title, TitleEmbedding, TitleKind
from phare.embeddings.service import EmbeddingService, build_embedding_text
from phare.providers.fakes import FakeLLMProvider


def _title(session: Session, **kwargs: object) -> Title:
    defaults = {
        "kind": TitleKind.movie,
        "title": "Dune",
        "year": 2021,
        "genres": ["Science Fiction"],
        "keywords": ["desert", "spice"],
        "overview": "Paul Atreides on Arrakis.",
    }
    defaults.update(kwargs)
    title = Title(**defaults)
    session.add(title)
    session.flush()
    return title


def test_build_embedding_text_includes_metadata() -> None:
    title = Title(
        kind=TitleKind.movie,
        title="Dune",
        year=2021,
        genres=["Science Fiction"],
        keywords=["desert"],
        overview="On Arrakis.",
    )
    text = build_embedding_text(title)
    assert "Dune" in text
    assert "2021" in text
    assert "Science Fiction" in text
    assert "desert" in text
    assert "On Arrakis." in text


def test_embed_missing_stores_vectors(db_session: Session) -> None:
    _title(db_session)
    _title(db_session, tmdb_id=1, title="Sicario")

    count = EmbeddingService(
        db_session, FakeLLMProvider(dim=EMBEDDING_DIM), "test-model"
    ).embed_missing()

    assert count == 2
    rows = db_session.scalars(select(TitleEmbedding)).all()
    assert len(rows) == 2
    assert all(r.model_version == "test-model" for r in rows)
    assert all(len(r.embedding) == EMBEDDING_DIM for r in rows)


def test_embed_missing_is_idempotent(db_session: Session) -> None:
    _title(db_session)
    llm = FakeLLMProvider(dim=EMBEDDING_DIM)
    service = EmbeddingService(db_session, llm, "test-model")

    first = service.embed_missing()
    second = service.embed_missing()

    assert first == 1
    assert second == 0  # nothing left to embed for this model version
    assert db_session.scalar(select(func.count()).select_from(TitleEmbedding)) == 1


def test_embed_missing_tolerates_a_concurrent_insert(db_session: Session) -> None:
    title = _title(db_session)
    service = EmbeddingService(db_session, FakeLLMProvider(dim=EMBEDDING_DIM), "test-model")
    assert service.embed_missing() == 1

    # Simulate a racing pass that snapshotted this title as "missing" before the first insert
    # committed: force it back into the to-embed set. ON CONFLICT DO NOTHING must swallow the
    # duplicate rather than raise IntegrityError, and must not write a second row.
    service._titles_missing_embedding = lambda limit=None: [title]  # type: ignore[method-assign]
    service.embed_missing()  # must not raise
    assert db_session.scalar(select(func.count()).select_from(TitleEmbedding)) == 1


def test_embed_missing_respects_limit(db_session: Session) -> None:
    for n in range(5):
        _title(db_session, tmdb_id=n, title=f"Film {n}")
    service = EmbeddingService(db_session, FakeLLMProvider(dim=EMBEDDING_DIM), "test-model")

    first = service.embed_missing(limit=2)  # bounded read-path top-up
    assert first == 2
    assert db_session.scalar(select(func.count()).select_from(TitleEmbedding)) == 2

    rest = service.embed_missing()  # unbounded authoritative pass clears the backlog
    assert rest == 3
    assert db_session.scalar(select(func.count()).select_from(TitleEmbedding)) == 5


def test_new_model_version_triggers_reembed(db_session: Session) -> None:
    _title(db_session)
    EmbeddingService(db_session, FakeLLMProvider(dim=EMBEDDING_DIM), "v1").embed_missing()

    reembedded = EmbeddingService(
        db_session, FakeLLMProvider(dim=EMBEDDING_DIM), "v2"
    ).embed_missing()

    assert reembedded == 1
    versions = set(db_session.scalars(select(TitleEmbedding.model_version)).all())
    assert versions == {"v1", "v2"}
