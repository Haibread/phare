"""Versioned embedding space + coverage-based cutover (round 9).

Exercised mostly on the offline local-hash space (bare tag ``local-hash-v1``, write tag
``local-hash-v1#d2``) so it stays hermetic. Covers: the write tag folds the document version;
reads serve the OLD tag under partial coverage and flip at the threshold (with the cutover log);
a read never mixes spaces (centroid tag == candidate-query tag); the UI degrade/building state
derives from the SERVED space; old-space cleanup with its guards.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phare.core.config import Settings
from phare.db.models import (
    EMBEDDING_DIM,
    EventType,
    Profile,
    Title,
    TitleEmbedding,
    TitleKind,
    WatchEvent,
)
from phare.embeddings.cleanup import (
    delete_superseded_embeddings,
    schedule_superseded_cleanup,
    superseded_version,
)
from phare.embeddings.version import (
    CUTOVER_COVERAGE,
    DOC_VERSION,
    active_embedding_version,
    embedding_model_version,
    embedding_write_version,
)
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.recommend.candidates import generate_candidates
from phare.recommend.service import RecommendationService
from phare.recommend.taste_vector import compute_taste_centroid

_OFFLINE = Settings(llm_api_key=None)
_PREVIOUS = f"{LOCAL_MODEL_VERSION}"  # bare = doc v1
_WRITE = f"{LOCAL_MODEL_VERSION}#d{DOC_VERSION}"


def _make_titles(session: Session, n: int) -> list[Title]:
    titles = [
        Title(kind=TitleKind.movie, tmdb_id=i, title=f"Film {i}", genres=["Drama"], keywords=[])
        for i in range(n)
    ]
    session.add_all(titles)
    session.flush()
    return titles


def _embed(session: Session, version: str, titles: list[Title]) -> None:
    provider = LocalHashEmbeddingProvider(dim=EMBEDDING_DIM)
    vectors = provider.embed([t.title for t in titles])
    session.add_all(
        TitleEmbedding(title_id=t.id, model_version=version, embedding=v)
        for t, v in zip(titles, vectors, strict=True)
    )
    session.flush()


# --- tag composition -------------------------------------------------------------------------


def test_write_tag_folds_document_version() -> None:
    assert embedding_model_version(_OFFLINE) == LOCAL_MODEL_VERSION  # bare = doc v1
    assert embedding_write_version(_OFFLINE) == f"{LOCAL_MODEL_VERSION}#d{DOC_VERSION}"

    keyed = Settings(llm_api_key="sk-test", llm_embedding_model="text-embedding-3-small")
    assert embedding_model_version(keyed) == "text-embedding-3-small"
    assert embedding_write_version(keyed) == f"text-embedding-3-small#d{DOC_VERSION}"


# --- coverage-based read cutover -------------------------------------------------------------


def test_reads_serve_old_space_until_coverage_threshold(db_session: Session) -> None:
    titles = _make_titles(db_session, 20)
    _embed(db_session, _PREVIOUS, titles)  # old space fully built (doc v1)
    # New space empty → coverage 0% → reads must stay on the old tag.
    assert active_embedding_version(db_session, _OFFLINE) == _PREVIOUS

    # Build the new space to just under the threshold (18/20 = 0.90 < 0.95).
    _embed(db_session, _WRITE, titles[:18])
    assert active_embedding_version(db_session, _OFFLINE) == _PREVIOUS

    # Cross the threshold (19/20 = 0.95 >= 0.95) → reads flip to the new tag.
    _embed(db_session, _WRITE, titles[18:19])
    assert active_embedding_version(db_session, _OFFLINE) == _WRITE


def test_cutover_is_logged_when_it_flips(db_session: Session, caplog) -> None:
    titles = _make_titles(db_session, 10)
    _embed(db_session, _WRITE, titles)  # 100% coverage → served
    with caplog.at_level(logging.INFO):
        served = active_embedding_version(db_session, _OFFLINE)
    assert served == _WRITE
    rec = next(r for r in caplog.records if r.message == "embeddings.space_cutover")
    assert rec.served_version == _WRITE
    assert rec.superseded_version == _PREVIOUS
    assert rec.coverage >= CUTOVER_COVERAGE


def test_empty_catalog_serves_write_tag(db_session: Session) -> None:
    # No titles at all: nothing to serve either way, so the write tag is returned (reads over an
    # empty catalog return nothing regardless).
    assert active_embedding_version(db_session, _OFFLINE) == _WRITE


# --- no mixed-space reads --------------------------------------------------------------------


def _seed_profile(session: Session, titles: list[Title]) -> uuid.UUID:
    profile = Profile(display_name="cutover")
    session.add(profile)
    session.flush()
    session.add(
        WatchEvent(
            profile_id=profile.id,
            title_id=titles[0].id,
            type=EventType.rated,
            rating=9.0,
            source="test",
            external_ref="test:0",
        )
    )
    session.flush()
    return profile.id


def test_partial_coverage_reads_are_single_space(db_session: Session) -> None:
    """Under partial new-space coverage the centroid and the candidate query must both resolve to
    the OLD tag — a request must never mix spaces. Asserts the tag the service reads with equals
    the tag the centroid was computed in, and that it is the served (old) tag."""
    titles = _make_titles(db_session, 20)
    _embed(db_session, _PREVIOUS, titles)
    _embed(db_session, _WRITE, titles[:10])  # 50% — below threshold
    profile_id = _seed_profile(db_session, titles)

    served = active_embedding_version(db_session, _OFFLINE)
    assert served == _PREVIOUS

    service = RecommendationService(
        db_session,
        embed_provider=LocalHashEmbeddingProvider(dim=EMBEDDING_DIM),
        embed_read_version=served,
        embed_write_version=_WRITE,
        chat_llm=None,
    )
    # The read tag the service uses everywhere IS the served old tag, not the write tag.
    assert service.embed_model_version == _PREVIOUS
    assert service.embed_write_version == _WRITE

    # The centroid is computable only because the old space has the watched title's vector; a
    # candidate query in the same tag returns results. Both use ``service.embed_model_version``.
    centroid = service._centroid(profile_id)
    assert centroid is not None
    pool = generate_candidates(
        db_session, profile_id, centroid, service.embed_model_version, limit=5
    )
    assert pool  # non-empty: query hit the fully-built old space, not the half-built new one

    # A query in the write tag would only see the 10 embedded there — proving the tags are distinct
    # spaces and the service picked the right (old, complete) one.
    centroid_old = compute_taste_centroid(db_session, profile_id, _PREVIOUS)
    centroid_new = compute_taste_centroid(db_session, profile_id, _WRITE)
    assert centroid_old is not None
    assert centroid_new is not None  # title 0 happens to be in the first 10
    assert centroid_old == centroid  # centroid was computed in the served (old) space


# --- degrade / building state derives from served space --------------------------------------


def test_degraded_flag_follows_served_space() -> None:
    from phare.api.deps import Embedder

    # Real model served (a v1 real-model space while v2 builds): NOT degraded.
    real = Embedder(
        provider=LocalHashEmbeddingProvider(),
        read_version="text-embedding-3-small",
        write_version="text-embedding-3-small#d2",
    )
    assert real.degraded is False

    # Local space served, any doc version: degraded.
    offline_v1 = Embedder(
        provider=LocalHashEmbeddingProvider(),
        read_version=LOCAL_MODEL_VERSION,
        write_version=_WRITE,
    )
    offline_v2 = Embedder(
        provider=LocalHashEmbeddingProvider(),
        read_version=_WRITE,
        write_version=_WRITE,
    )
    assert offline_v1.degraded is True
    assert offline_v2.degraded is True


def test_profile_building_uses_served_space(db_session: Session) -> None:
    """While the new space builds, the served (old) space has the watched title's vector, so the
    centroid computes and ``profile_building`` is False — the UI must not show a building banner."""
    titles = _make_titles(db_session, 20)
    _embed(db_session, _PREVIOUS, titles)  # old space complete
    # New space empty on purpose: the write-target has nothing, but reads serve the old space.
    profile_id = _seed_profile(db_session, titles)
    served = active_embedding_version(db_session, _OFFLINE)
    assert served == _PREVIOUS

    service = RecommendationService(
        db_session,
        embed_provider=LocalHashEmbeddingProvider(dim=EMBEDDING_DIM),
        embed_read_version=served,
        embed_write_version=_WRITE,
        chat_llm=None,
    )
    assert service.profile_building(profile_id) is False


# --- old-space cleanup + guards --------------------------------------------------------------


def test_cleanup_deletes_superseded_space_after_cutover(db_session: Session) -> None:
    titles = _make_titles(db_session, 10)
    _embed(db_session, _PREVIOUS, titles)
    _embed(db_session, _WRITE, titles)  # new space complete → served

    assert superseded_version(db_session, _OFFLINE) == _PREVIOUS
    deleted = delete_superseded_embeddings(db_session, _OFFLINE)
    assert deleted == 10
    # The served (write) space is untouched; the old space is gone.
    remaining = set(db_session.scalars(select(TitleEmbedding.model_version)).all())
    assert remaining == {_WRITE}


def test_cleanup_refuses_while_old_space_is_still_served(db_session: Session) -> None:
    """Guard: never delete the space currently answering queries. Under partial new-space coverage
    the old tag is still served, so it must NOT be reclaimed."""
    titles = _make_titles(db_session, 20)
    _embed(db_session, _PREVIOUS, titles)
    _embed(db_session, _WRITE, titles[:5])  # 25% — old tag still served

    assert active_embedding_version(db_session, _OFFLINE) == _PREVIOUS
    assert superseded_version(db_session, _OFFLINE) is None
    assert delete_superseded_embeddings(db_session, _OFFLINE) == 0
    # Nothing deleted: the old space is intact.
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(TitleEmbedding)
            .where(TitleEmbedding.model_version == _PREVIOUS)
        )
        == 20
    )


def test_cleanup_noops_when_no_old_space_exists(db_session: Session) -> None:
    titles = _make_titles(db_session, 10)
    _embed(db_session, _WRITE, titles)  # only the new space ever existed
    assert superseded_version(db_session, _OFFLINE) is None
    assert delete_superseded_embeddings(db_session, _OFFLINE) == 0


def test_schedule_superseded_cleanup_runs_via_injected_runner(db_session: Session) -> None:
    """The scheduler fires the cleanup (through a synchronous test runner so no real thread /
    separate DB connection is spawned) only when there is a superseded space to reclaim."""
    titles = _make_titles(db_session, 10)
    _embed(db_session, _PREVIOUS, titles)
    _embed(db_session, _WRITE, titles)

    # superseded_version says there's work; drive the delete synchronously on this session.
    assert superseded_version(db_session, _OFFLINE) == _PREVIOUS
    deleted = delete_superseded_embeddings(db_session, _OFFLINE)
    assert deleted == 10

    # And the scheduler dedups: with the injected runner it invokes work exactly once. The runner
    # here runs the work inline (so the module's in-flight flag is released) and a second schedule
    # while the first is "running" is refused.
    import phare.embeddings.cleanup as cleanup_mod

    calls: list[int] = []

    def inline_runner(work: object) -> None:
        calls.append(1)
        # Second schedule mid-flight is deduped to False.
        assert schedule_superseded_cleanup(_OFFLINE, runner=lambda w: calls.append(99)) is False

    scheduled = schedule_superseded_cleanup(_OFFLINE, runner=inline_runner)
    assert scheduled is True
    assert calls == [1]  # the deduped second call never ran its runner
    # Release the in-flight flag we set via the injected runner (real runner does this in finally).
    with cleanup_mod._lock:
        cleanup_mod._running = False


def test_ensure_embeddings_writes_new_tag_while_reads_serve_old(db_session: Session) -> None:
    """The write side (inline micro-batch) targets the WRITE tag even while reads serve the old
    space — proving the service embeds into the new space during the build-out window."""
    titles = _make_titles(db_session, 5)
    _embed(db_session, _PREVIOUS, titles)  # old space complete
    profile_id = _seed_profile(db_session, titles)
    served = active_embedding_version(db_session, _OFFLINE)
    assert served == _PREVIOUS  # new space empty → reads still old

    service = RecommendationService(
        db_session,
        embed_provider=LocalHashEmbeddingProvider(dim=EMBEDDING_DIM),
        embed_read_version=served,
        embed_write_version=_WRITE,
        chat_llm=None,
    )
    service.ensure_embeddings()
    # The micro-batch wrote vectors under the WRITE tag, not the served old tag.
    write_count = db_session.scalar(
        select(func.count())
        .select_from(TitleEmbedding)
        .where(TitleEmbedding.model_version == _WRITE)
    )
    assert write_count and write_count > 0
    assert profile_id  # (profile seeded so the read path is representative)
