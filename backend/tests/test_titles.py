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


def _meta(runtime_minutes: int | None = None) -> TitleMetadata:
    return TitleMetadata(
        kind=TitleKind.movie,
        tmdb_id=42,
        title="Dune",
        overview=_LOCALIZED_OVERVIEW,
        genres=["Science-Fiction"],
        runtime_minutes=runtime_minutes,
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


def _seed_title(
    session: Session, *, runtime_minutes: int | None = None, tmdb_id: int | None = 42
) -> Title:
    title = Title(
        kind=TitleKind.movie,
        title="Dune",
        year=2021,
        tmdb_id=tmdb_id,
        genres=["Sci-Fi"],
        keywords=["desert"],
        overview=_BASE_OVERVIEW,
        runtime_minutes=runtime_minutes,
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
    # Runtime is already known, so the runtime backfill is a no-op and this isolates the
    # localization cache: the second open must serve the synopsis with no TMDB fetch at all.
    title = _seed_title(db_session, runtime_minutes=155)
    provider = _CountingProvider(_meta(runtime_minutes=155))
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


def test_detail_backfills_missing_runtime_from_tmdb(db_session: Session) -> None:
    # The bug: a discover-imported mainstream title (Inception) sits at runtime_minutes = NULL, and
    # the detail read never healed it — the recommend path only backfills for runtime-cap turns. The
    # detail open must fill it lazily from TMDB and persist it.
    title = _seed_title(db_session, runtime_minutes=None)
    provider = _CountingProvider(_meta(runtime_minutes=148))

    body = _client(db_session, provider).get(f"/titles/{title.id}").json()

    assert body["runtimeMinutes"] == 148
    # Persisted permanently: a fresh read of the row sees the filled runtime.
    db_session.expire(title)
    assert db_session.get(Title, title.id).runtime_minutes == 148


def test_detail_backfills_runtime_from_a_warm_localization_cache(db_session: Session) -> None:
    # The fallback path: localization is already cached (so it serves without a TMDB fetch), but the
    # runtime is still NULL. The dedicated backfill fetch must heal it even though localization
    # didn't consult the provider this request.
    title = _seed_title(db_session, runtime_minutes=None)
    # Warm the localization cache without ever recording a runtime (meta carries none).
    _client(db_session, _CountingProvider(_meta())).get(f"/titles/{title.id}")
    db_session.expire(title)
    assert (
        db_session.get(Title, title.id).runtime_minutes is None
    )  # still NULL after the first open

    provider = _CountingProvider(_meta(runtime_minutes=148))
    body = _client(db_session, provider).get(f"/titles/{title.id}").json()

    assert body["runtimeMinutes"] == 148
    assert provider.calls == 1  # exactly one fetch, and it healed the runtime


def test_detail_does_not_refetch_runtime_when_already_known(db_session: Session) -> None:
    # Runtime already present → no runtime fetch is spent for it. The localization fetch may still
    # happen; what matters is the stored runtime survives (not overwritten by the provider's value).
    title = _seed_title(db_session, runtime_minutes=120)
    provider = _CountingProvider(_meta(runtime_minutes=999))

    body = _client(db_session, provider).get(f"/titles/{title.id}").json()

    assert body["runtimeMinutes"] == 120  # the stored runtime, not the provider's 999


def test_detail_runtime_survives_a_tmdb_outage(db_session: Session) -> None:
    # A flaky TMDB during the backfill must not break the detail view — runtime just stays NULL.
    title = _seed_title(db_session, runtime_minutes=None)

    resp = _client(db_session, _FailingProvider()).get(f"/titles/{title.id}")

    assert resp.status_code == 200
    assert resp.json()["runtimeMinutes"] is None


def test_runtime_backfill_still_gets_its_attempt_when_localization_errored(
    db_session: Session,
) -> None:
    # Review finding (round 4): a TMDB exception during localization used to return the
    # "already fetched" flag as True, so the runtime backfill was skipped even though no
    # metadata was ever obtained. A transient blip mid-request must not cost the runtime
    # heal: the first call (localization) fails, the second (backfill) succeeds.
    class _FlakyThenHealthy:
        def __init__(self) -> None:
            self.calls = 0

        def get_title(self, tmdb_id: int, kind: TitleKind) -> TitleMetadata | None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("tmdb blip")
            return _meta(runtime_minutes=148)

    title = _seed_title(db_session, runtime_minutes=None)
    provider = _FlakyThenHealthy()

    body = _client(db_session, provider).get(f"/titles/{title.id}").json()

    assert provider.calls == 2  # localization failed, backfill still tried
    assert body["runtimeMinutes"] == 148


def test_detail_runtime_stays_null_without_a_tmdb_key(db_session: Session) -> None:
    title = _seed_title(db_session, runtime_minutes=None)

    body = _client(db_session, None).get(f"/titles/{title.id}").json()

    assert body["runtimeMinutes"] is None  # offline: no backfill, no crash


def test_unknown_title_is_404(db_session: Session) -> None:
    import uuid

    resp = _client(db_session, None).get(f"/titles/{uuid.uuid4()}")

    assert resp.status_code == 404
