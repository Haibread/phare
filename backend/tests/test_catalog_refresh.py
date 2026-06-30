"""Ongoing catalog freshness: the incremental pull of current/new releases + the scheduler guard.

Kept hermetic — the real pull hits TMDB, so we exercise the import/embed core with a fake source and
the scheduler's disabled-path guard, never the network."""

from __future__ import annotations

from sqlalchemy.orm import Session

from phare.catalog.bootstrap import candidate_pool_size
from phare.catalog.refresh import run_refresh, start_refresh_loop
from phare.core.config import Settings
from phare.db.models import TitleKind
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION, LocalHashEmbeddingProvider
from phare.providers.types import TitleMetadata


class _FakeRefreshSource:
    """Returns fixed trending + now-playing movie pages (and nothing for TV / later pages)."""

    def __init__(self, trending: list[TitleMetadata], now_playing: list[TitleMetadata]) -> None:
        self._trending = trending
        self._now_playing = now_playing

    def trending(self, kind: TitleKind, page: int = 1) -> list[TitleMetadata]:
        return self._trending if (kind is TitleKind.movie and page == 1) else []

    def now_playing(self, kind: TitleKind, page: int = 1) -> list[TitleMetadata]:
        return self._now_playing if (kind is TitleKind.movie and page == 1) else []


def _meta(tmdb_id: int, title: str) -> TitleMetadata:
    return TitleMetadata(
        kind=TitleKind.movie, tmdb_id=tmdb_id, title=title, overview="A current release."
    )


def test_run_refresh_imports_current_releases_deduped(db_session: Session) -> None:
    trending = [_meta(8000, "Trend A"), _meta(8001, "Trend B")]
    # 8001 also shows up in now-playing (overlap is normal) → deduped; 8100 is new.
    now_playing = [_meta(8001, "Trend B again"), _meta(8100, "Now Playing")]

    created, embedded = run_refresh(
        db_session,
        _FakeRefreshSource(trending, now_playing),
        LocalHashEmbeddingProvider(),
        LOCAL_MODEL_VERSION,
    )
    assert created == 3  # 8000, 8001, 8100 — the duplicate 8001 collapsed
    assert embedded >= 3
    assert candidate_pool_size(db_session) == 3  # all freshly imported titles are recommendable


def test_run_refresh_drops_titles_without_an_overview(db_session: Session) -> None:
    # The overview is the embedding input; a release with none is noise, so it's skipped.
    trending = [
        _meta(8200, "Has overview"),
        TitleMetadata(kind=TitleKind.movie, tmdb_id=8201, title="No overview"),
    ]
    created, _ = run_refresh(
        db_session,
        _FakeRefreshSource(trending, []),
        LocalHashEmbeddingProvider(),
        LOCAL_MODEL_VERSION,
    )
    assert created == 1


def test_start_refresh_loop_is_a_noop_when_disabled() -> None:
    # Interval 0 disables the scheduler entirely — no thread, and stop() is safe to call.
    stop = start_refresh_loop(Settings(catalog_refresh_interval_seconds=0))
    stop()
