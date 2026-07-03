"""M4.1 [C1]: the read path never embeds a whole import inline.

``ensure_embeddings`` embeds only a small, time-bounded micro-batch and hands any larger backlog
to a single background backfill. These tests drive the "background" work synchronously (the real
scheduler spawns a daemon thread with its own DB connection, which a test can't roll back).
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import phare.embeddings.backfill as backfill
from phare.db.models import EMBEDDING_DIM, Title, TitleEmbedding, TitleKind
from phare.embeddings.backfill import (
    embedding_backfill_running,
    run_embedding_backfill,
    schedule_embedding_backfill,
)
from phare.embeddings.service import EmbeddingService
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.fakes import FakeLLMProvider
from phare.recommend.service import READ_EMBED_MICRO_LIMIT, RecommendationService

MODEL = LOCAL_MODEL_VERSION


@pytest.fixture(autouse=True)
def _reset_backfill_flag() -> None:
    """The in-process "one backfill at a time" flag is module state — reset it so a test that
    parks a runner (to prove the lock) can't leak a stuck ``running`` into the next test."""
    yield
    with backfill._lock:
        backfill._running = False


def _service(session: Session, provider: object) -> RecommendationService:
    return RecommendationService(
        session, embed_provider=provider, embed_model_version=MODEL, chat_llm=None
    )


def _seed_titles(session: Session, n: int) -> None:
    for i in range(n):
        session.add(
            Title(
                kind=TitleKind.movie,
                title=f"Film {i}",
                year=2000 + (i % 20),
                genres=["Drama"],
                keywords=["k"],
                overview=f"Story number {i}",
                tmdb_id=100_000 + i,
            )
        )
    session.flush()


def _embedded_count(session: Session) -> int:
    return session.scalar(
        select(func.count())
        .select_from(TitleEmbedding)
        .where(TitleEmbedding.model_version == MODEL)
    )


def _has_missing(session: Session) -> bool:
    return EmbeddingService(session, LocalHashEmbeddingProvider(), MODEL).has_missing()


def test_read_path_embeds_only_a_micro_batch(db_session: Session, monkeypatch) -> None:
    # 100 unembedded titles: the read path must embed only the micro-batch (≤16) inline and defer
    # the rest, never embed the whole import synchronously (review C1).
    _seed_titles(db_session, 100)
    scheduled: list[tuple[object, str]] = []
    monkeypatch.setattr(
        "phare.recommend.service.schedule_embedding_backfill",
        lambda provider, version, **kw: (scheduled.append((provider, version)), True)[1],
    )

    embedded = _service(db_session, LocalHashEmbeddingProvider()).ensure_embeddings()

    assert embedded <= READ_EMBED_MICRO_LIMIT
    assert _embedded_count(db_session) == embedded  # only the micro-batch reached the DB
    assert len(scheduled) == 1  # the backlog was handed off to a background backfill
    assert _has_missing(db_session)  # most of the catalog is still unembedded


def test_read_path_stays_fast_with_a_slow_provider(db_session: Session, monkeypatch) -> None:
    # A deliberately slow embedder: embedding all 100 titles inline would be many seconds; the
    # bounded micro-batch keeps the read path snappy regardless.
    _seed_titles(db_session, 100)
    monkeypatch.setattr(
        "phare.recommend.service.schedule_embedding_backfill", lambda *a, **kw: True
    )
    slow = FakeLLMProvider(dim=EMBEDDING_DIM, embed_delay=0.02)

    start = time.monotonic()
    embedded = _service(db_session, slow).ensure_embeddings()
    elapsed = time.monotonic() - start

    assert embedded <= READ_EMBED_MICRO_LIMIT
    # Micro-batch is a couple of embed calls; embedding all 100 would be ~13. Comfortably bounded.
    assert elapsed < 1.0


def test_embed_missing_honours_the_time_budget(db_session: Session) -> None:
    # The wall-clock guard stops between batches: the first batch always runs, a spent budget
    # forbids the rest — so a slow provider can't blow the read-path budget title by title.
    _seed_titles(db_session, 100)
    slow = FakeLLMProvider(dim=EMBEDDING_DIM, embed_delay=0.05)

    embedded = EmbeddingService(db_session, slow, MODEL).embed_missing(
        batch_size=8, limit=100, time_budget_s=0.01
    )

    assert embedded == 8  # exactly the first batch; the tiny budget cut the second


def test_background_backfill_embeds_the_whole_backlog(db_session: Session) -> None:
    # The background pass is unbounded (no 512 cap): it clears the entire missing-vector backlog.
    _seed_titles(db_session, 100)

    embedded = run_embedding_backfill(db_session, LocalHashEmbeddingProvider(), MODEL)

    assert embedded == 100
    assert not _has_missing(db_session)


def test_only_one_backfill_runs_at_a_time() -> None:
    # Two concurrent read requests must not fan out two backfills — the in-process lock dedupes.
    parked: list[object] = []

    def parking_runner(work: object) -> None:
        parked.append(work)  # capture but never run → the "backfill" stays in flight

    first = schedule_embedding_backfill(LocalHashEmbeddingProvider(), MODEL, runner=parking_runner)
    second = schedule_embedding_backfill(LocalHashEmbeddingProvider(), MODEL, runner=parking_runner)

    assert first is True  # this call started the backfill
    assert second is False  # already running → skipped
    assert embedding_backfill_running() is True
    assert len(parked) == 1  # only the first scheduled any work


def test_rows_serve_the_full_catalog_after_the_backfill(db_session: Session) -> None:
    # End to end: a fresh import lands partially embedded on first read, then the background pass
    # fills the rest so the next read sees the whole space. The autouse conftest fixture makes the
    # scheduler synchronous, standing in for the completed daemon thread.
    _seed_titles(db_session, 100)
    service = _service(db_session, LocalHashEmbeddingProvider())

    service.ensure_embeddings()  # micro-batch inline + (synchronous) backfill of the rest

    assert not _has_missing(db_session)
    assert _embedded_count(db_session) == 100
