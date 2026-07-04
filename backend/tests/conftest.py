"""Test fixtures. DB-backed tests run against a disposable ``phare_test`` database and
skip cleanly when no Postgres server is reachable.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from phare.api.app import create_app
from phare.core.auth import get_current_user
from phare.core.config import get_settings
from phare.db import models  # noqa: F401  (registers tables on Base.metadata)
from phare.db.base import Base, get_session
from phare.db.models import Profile, User

# Hermetic tests: a developer's real .env must not change test behavior (offline by default) or
# make the suite hit the network. Env vars override .env in pydantic-settings, so setting them here
# wins over the file. Done unless the opt-in live tests are requested (PHARE_LIVE_LLM=1).
if not os.environ.get("PHARE_LIVE_LLM"):
    # Cut the repo-root .env off at the root: pydantic-settings' env_file is a *fallback* consulted
    # whenever a var is absent from the environment, so blanking vars below isn't enough — a test
    # that delenv()s one (e.g. test_trakt_oauth's "requires credentials") re-exposes the .env value
    # and reads the developer's live creds. Pointing model_config.env_file at nothing makes every
    # Settings()/get_settings() in the suite ignore the file entirely, for *all* vars, not just the
    # ones enumerated below. (The live-LLM opt-in keeps the .env so those tests can use real creds.)
    from phare.core.config import Settings

    Settings.model_config["env_file"] = None
    # Credentials: blank to empty (these are str|None, so "" reads as unset = offline).
    for _var in (
        "LLM_API_KEY",
        "TMDB_API_KEY",
        "TRAKT_CLIENT_ID",
        "TRAKT_CLIENT_SECRET",
        "SEERR_BASE_URL",
        "SEERR_API_KEY",
        "SECRET_KEY",
        "PLEX_CLIENT_IDENTIFIER",
    ):
        os.environ[_var] = ""
    # Behaviour-gating flags a dev flips locally: pin to their secure defaults so tests that assert
    # the closed-by-default posture don't fail on a dev box. Typed (bool/int), so "" won't parse —
    # set the actual default value.
    # Rate limiting is off by default in tests so a suite that hammers one endpoint through a single
    # app instance doesn't trip it; the rate-limit test flips it on explicitly.
    for _var, _default in (("REGISTRATION_OPEN", "false"), ("RATE_LIMIT_ENABLED", "false")):
        os.environ[_var] = _default
    get_settings.cache_clear()

_BASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://phare:phare@localhost:5432/phare"
)
_TEST_DB = "phare_test"

# Monotonic sequence for default account emails — see make_account. Keeps independent tests from
# colliding on one login identity without threading a unique email through every call site.
_account_email_seq = itertools.count(1)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = make_url(_BASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        connection = admin.connect()
    except OperationalError:
        pytest.skip("Postgres not reachable; skipping DB-backed tests")
    with connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {_TEST_DB}"))

    test_engine = create_engine(url.set(database=_TEST_DB))
    with test_engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(test_engine)
    yield test_engine
    test_engine.dispose()
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB} WITH (FORCE)"))
    admin.dispose()


def make_account(
    session: Session,
    *,
    display_name: str = "me",
    email: str | None = None,
    is_admin: bool = True,
) -> User:
    """Create a User and its 1:1 Profile directly in the DB (auth-bypassing test setup).

    ``email`` defaults to a fresh unique address per call, so independent tests can't collide on the
    same login identity if a rollback ever fails to isolate them; pass an explicit value when a test
    needs a known or shared address (e.g. the multi-account isolation checks).
    """
    if email is None:
        email = f"user{next(_account_email_seq)}@example.test"
    user = User(email=email, display_name=display_name, is_admin=is_admin)
    session.add(user)
    session.flush()
    session.add(Profile(user_id=user.id, display_name=display_name))
    session.flush()
    return user


def authed_client(
    session: Session,
    user: User,
    overrides: dict[Callable[..., object], Callable[[], object]] | None = None,
) -> TestClient:
    """A TestClient acting as ``user`` — overrides the session + auth dependency (no real token).

    Pass ``overrides`` as ``{dependency_callable: factory}`` for per-test fakes (e.g. an LLM
    provider). For token-path tests, build the client without this and send a real
    ``Authorization`` header instead.
    """
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: user
    for dependency, factory in (overrides or {}).items():
        app.dependency_overrides[dependency] = factory
    return TestClient(app)


def unauthed_app(session: Session) -> FastAPI:
    """An app wired to the test session but with auth left intact (for token-path / 401 tests)."""
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest.fixture
def account(db_session: Session) -> User:
    """A ready-made admin account (User + Profile) for the common single-user test."""
    return make_account(db_session)


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """A session wrapped in a transaction that is rolled back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    # create_savepoint: a commit() inside request handlers becomes a SAVEPOINT release,
    # so the outer rollback still undoes everything and tests stay isolated.
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(autouse=True)
def _sync_embedding_backfill(request: pytest.FixtureRequest) -> None:
    """Keep the "background" embedding backfill synchronous and on the test's own session.

    In production ``ensure_embeddings`` embeds a small inline micro-batch and hands the rest to a
    daemon thread with its *own* DB connection (review C1). In tests that thread would commit
    outside the per-test rollback and race the assertions, so we replace the scheduler with a
    synchronous pass on the test session — the catalog still fully embeds inline, exactly as the
    old unbounded top-up did. Only touches DB-backed tests (those that pull in ``db_session``);
    tests that exercise the scheduler itself re-patch it in their own body (last patch wins).
    """
    if "db_session" not in request.fixturenames:
        return
    from phare.embeddings.service import EmbeddingService

    session = request.getfixturevalue("db_session")

    def _sync(provider: object, model_version: str, *, runner: object = None) -> bool:
        EmbeddingService(session, provider, model_version).embed_missing()  # type: ignore[arg-type]
        return True

    monkeypatch = request.getfixturevalue("monkeypatch")
    monkeypatch.setattr("phare.recommend.service.schedule_embedding_backfill", _sync)


@pytest.fixture(autouse=True)
def _sync_taste_refresh(request: pytest.FixtureRequest) -> None:
    """Keep the "background" taste refresh synchronous and on the test's own session (review C3).

    Same rationale as the embedding backfill: the real scheduler spawns a daemon thread with its own
    connection. Offline (the default hermetic suite) it's a no-op — no LLM, nothing to extract — so
    this only bites when a test wires an LLM in. Tests exercising the scheduler re-patch it."""
    if "db_session" not in request.fixturenames:
        return
    from phare.taste.backfill import run_taste_refresh

    session = request.getfixturevalue("db_session")

    def _sync(
        profile_id: object,
        llm: object,
        model_version: str,
        language: object = "en",
        *,
        runner: object = None,
    ) -> bool:
        if llm is None:
            return False
        run_taste_refresh(session, profile_id, llm, model_version, language)  # type: ignore[arg-type]
        return True

    monkeypatch = request.getfixturevalue("monkeypatch")
    monkeypatch.setattr("phare.api.profiles.schedule_taste_refresh", _sync)
