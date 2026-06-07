"""Database engine, session factory, and declarative base."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from phare.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the lazily-created process-wide engine."""
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _session_factory


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yield a session bound to the request."""
    with get_session_factory()() as session:
        yield session
