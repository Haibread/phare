"""Test fixtures. DB-backed tests run against a disposable ``phare_test`` database and
skip cleanly when no Postgres server is reachable.
"""

from __future__ import annotations

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
    for _var, _default in (("REGISTRATION_OPEN", "false"),):
        os.environ[_var] = _default
    get_settings.cache_clear()

_BASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://phare:phare@localhost:5432/phare"
)
_TEST_DB = "phare_test"


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
    email: str | None = "me@example.test",
    is_admin: bool = True,
) -> User:
    """Create a User and its 1:1 Profile directly in the DB (auth-bypassing test setup)."""
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
