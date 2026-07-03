"""M4.2 [C2]: the title detail view caches its localized synopsis/genres in the DB.

Opening a card used to fetch the localized synopsis from TMDB live every time (~6 s). The cache
means a second open hits no TMDB, and a TMDB outage is absorbed by the stored copy.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from phare.api.deps import get_optional_metadata_provider
from phare.db.models import Title, TitleKind, TitleLocalization
from phare.providers.types import TitleMetadata
from tests.conftest import authed_client, make_account

_BASE_OVERVIEW = "Base-language overview."
_LOCALIZED_OVERVIEW = "Localized synopsis in the request language."


def _meta() -> TitleMetadata:
    return TitleMetadata(
        kind=TitleKind.movie,
        tmdb_id=42,
        title="Dune",
        overview=_LOCALIZED_OVERVIEW,
        genres=["Science-Fiction"],
    )


class _CountingProvider:
    """A metadata provider that records how many TMDB fetches it was asked to make."""

    def __init__(self, meta: TitleMetadata | None) -> None:
        self._meta = meta
        self.calls = 0

    def get_title(self, tmdb_id: int, kind: TitleKind) -> TitleMetadata | None:
        self.calls += 1
        return self._meta


class _FailingProvider:
    """Stands in for TMDB being unreachable."""

    def get_title(self, tmdb_id: int, kind: TitleKind) -> TitleMetadata | None:
        raise RuntimeError("tmdb unreachable")


def _client(session: Session, provider: object) -> TestClient:
    # Title routes are gated by get_current_user; authed_client overrides it. The metadata provider
    # is injected so a test can count TMDB fetches or simulate an outage.
    user = make_account(session)
    return authed_client(
        session, user, overrides={get_optional_metadata_provider: lambda: provider}
    )


def _seed_title(session: Session) -> Title:
    title = Title(
        kind=TitleKind.movie,
        title="Dune",
        year=2021,
        tmdb_id=42,
        genres=["Sci-Fi"],
        keywords=["desert"],
        overview=_BASE_OVERVIEW,
    )
    session.add(title)
    session.flush()
    return title


def test_first_open_localizes_and_caches(db_session: Session) -> None:
    title = _seed_title(db_session)
    provider = _CountingProvider(_meta())

    body = _client(db_session, provider).get(f"/titles/{title.id}").json()

    assert body["overview"] == _LOCALIZED_OVERVIEW
    assert body["genres"] == ["Science-Fiction"]
    assert provider.calls == 1
    cached = db_session.get(TitleLocalization, {"title_id": title.id, "language": "en"})
    assert cached is not None and cached.overview == _LOCALIZED_OVERVIEW


def test_second_open_serves_cache_without_touching_tmdb(db_session: Session) -> None:
    title = _seed_title(db_session)
    provider = _CountingProvider(_meta())
    client = _client(db_session, provider)

    first = client.get(f"/titles/{title.id}").json()
    second = client.get(f"/titles/{title.id}").json()

    assert first["overview"] == second["overview"] == _LOCALIZED_OVERVIEW
    assert provider.calls == 1  # the second open was a pure cache hit — no TMDB fetch


def test_outage_falls_back_to_the_stored_copy(db_session: Session) -> None:
    title = _seed_title(db_session)
    # First open fills the cache from a healthy TMDB.
    _client(db_session, _CountingProvider(_meta())).get(f"/titles/{title.id}")
    # Age the cache past any TTL so the next open actually tries TMDB (which is now down).
    cached = db_session.get(TitleLocalization, {"title_id": title.id, "language": "en"})
    cached.fetched_at = datetime(2000, 1, 1, tzinfo=UTC)
    db_session.flush()

    body = _client(db_session, _FailingProvider()).get(f"/titles/{title.id}").json()

    # Served from the stale cache rather than the wrong-language base overview.
    assert body["overview"] == _LOCALIZED_OVERVIEW


def test_no_tmdb_provider_serves_base_metadata(db_session: Session) -> None:
    title = _seed_title(db_session)

    body = _client(db_session, None).get(f"/titles/{title.id}").json()

    assert body["overview"] == _BASE_OVERVIEW  # offline: no localization, no crash
    assert db_session.get(TitleLocalization, {"title_id": title.id, "language": "en"}) is None


def test_unknown_title_is_404(db_session: Session) -> None:
    import uuid

    resp = _client(db_session, None).get(f"/titles/{uuid.uuid4()}")

    assert resp.status_code == 404
