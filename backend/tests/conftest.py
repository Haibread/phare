"""Test fixtures. DB-backed tests run against a disposable ``phare_test`` database and
skip cleanly when no Postgres server is reachable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from phare.db import models  # noqa: F401  (registers tables on Base.metadata)
from phare.db.base import Base

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


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """A session wrapped in a transaction that is rolled back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
