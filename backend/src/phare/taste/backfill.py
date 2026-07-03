"""Background taste extraction — keeps the slow LLM pass off the request that triggers it.

Seeding sample data (or any first import) used to run taste extraction *synchronously*, so the
"explore with sample data" call blocked ~28 s on the LLM before the app would reveal (review C3).
Now the caller seeds history, commits, and hands taste extraction to a background thread; the user
lands as soon as the catalog + history exist, and the taste profile fills in behind them (the
"building your profile" state — M3.12 — covers the gap). One refresh per profile at a time, guarded
by an in-process lock (single-process app). No queue, no worker component.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

from phare.core.i18n import DEFAULT_LANGUAGE, Language
from phare.providers.types import LLMProvider
from phare.taste.service import TasteService

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_running: set[uuid.UUID] = set()


def taste_refresh_running(profile_id: uuid.UUID) -> bool:
    """True while a background taste refresh is in flight for this profile."""
    with _lock:
        return profile_id in _running


def run_taste_refresh(
    session: object,
    profile_id: uuid.UUID,
    llm: LLMProvider,
    model_version: str,
    language: Language,
) -> None:
    """Extract + persist a profile's taste on the given session. The caller owns the commit.

    Split from the thread wrapper so tests can drive it synchronously on a rolled-back session
    rather than spawning a real thread with its own (un-rolled-back) DB connection.
    """
    TasteService(session, llm, model_version, language).generate(profile_id)  # type: ignore[arg-type]


def schedule_taste_refresh(
    profile_id: uuid.UUID,
    llm: LLMProvider | None,
    model_version: str,
    language: Language = DEFAULT_LANGUAGE,
    *,
    runner: Callable[[Callable[[], None]], None] | None = None,
) -> bool:
    """Ensure a background taste refresh runs for this profile.

    Returns True if this call started one, False if it was skipped — no LLM configured (offline: the
    deterministic centroid still personalises) or a refresh is already running for the profile.
    ``runner`` executes the work and defaults to a daemon thread; tests pass their own.
    """
    if llm is None:
        return False
    with _lock:
        if profile_id in _running:
            return False
        _running.add(profile_id)
    (runner or _spawn_daemon)(lambda: _run_backfill(profile_id, llm, model_version, language))
    return True


def _spawn_daemon(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="taste-refresh", daemon=True).start()


def _run_backfill(
    profile_id: uuid.UUID, llm: LLMProvider, model_version: str, language: Language
) -> None:
    try:
        from phare.db.base import get_session_factory

        with get_session_factory()() as session:
            run_taste_refresh(session, profile_id, llm, model_version, language)
            session.commit()
        logger.info("taste.backfill.done", extra={"profile_id": str(profile_id)})
    except Exception:  # noqa: BLE001 - a taste refresh must never crash the process
        logger.warning("taste.backfill.failed", extra={"profile_id": str(profile_id)})
    finally:
        with _lock:
            _running.discard(profile_id)
