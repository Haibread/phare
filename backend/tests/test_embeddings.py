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


def test_build_embedding_text_document_v2_full_pin() -> None:
    """Exact string pin for a fully-populated title (document v2). Order is load-bearing: short
    high-signal facets first, credits + language, then the free-text overview last."""
    title = Title(
        kind=TitleKind.movie,
        title="Spirited Away",
        year=2001,
        genres=["Animation", "Fantasy"],
        keywords=["bathhouse", "spirits"],
        directors=["Hayao Miyazaki"],
        top_cast=["Rumi Hiiragi", "Miyu Irino"],
        original_language="ja",
        overview="A girl wanders into a world of spirits.",
    )
    assert build_embedding_text(title) == (
        "Spirited Away\n"
        "Year: 2001\n"
        "Genres: Animation, Fantasy\n"
        "Keywords: bathhouse, spirits\n"
        "Directed by: Hayao Miyazaki\n"
        "Cast: Rumi Hiiragi, Miyu Irino\n"
        "Language: ja\n"
        "A girl wanders into a world of spirits."
    )


def test_build_embedding_text_document_v2_sparse_pin() -> None:
    """A sparse title omits every missing facet — no empty ``Cast:`` / ``Language:`` lines."""
    title = Title(kind=TitleKind.movie, title="Untitled", genres=[], keywords=[])
    assert build_embedding_text(title) == "Untitled"


def test_build_embedding_text_language_separates_anime_from_western_animation() -> None:
    """The language line is what pulls "ja" Animation away from "en" Animation in the space."""
    common = {"kind": TitleKind.movie, "genres": ["Animation"], "keywords": []}
    anime = Title(title="A", original_language="ja", **common)  # type: ignore[arg-type]
    western = Title(title="A", original_language="en", **common)  # type: ignore[arg-type]
    assert "Language: ja" in build_embedding_text(anime)
    assert "Language: en" in build_embedding_text(western)
    assert build_embedding_text(anime) != build_embedding_text(western)


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
