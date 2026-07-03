"""Background embedding backfill — keeps large imports off the read path.

The read path embeds only a tiny inline micro-batch; anything larger is handed here, to a single
background thread that embeds the whole missing-vector backlog in batches. An in-process lock
guarantees one backfill at a time — the app is single-process, so an in-memory guard is enough
(review C1 / M3). No queue, no worker component: the request that noticed the gap starts a daemon
thread and returns immediately.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from phare.embeddings.service import EmbeddingService
from phare.providers.types import LLMProvider

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_running = False


def embedding_backfill_running() -> bool:
    """True while a background backfill is in flight (single-process, in-memory state)."""
    with _lock:
        return _running


def run_embedding_backfill(session: object, provider: LLMProvider, model_version: str) -> int:
    """Embed the entire missing-vector backlog on the given session. The caller owns the commit.

    Split out from the thread wrapper so tests can drive it synchronously on a rolled-back session
    instead of spawning a real thread with its own (uncontrolled, un-rolled-back) DB connection.
    """
    return EmbeddingService(session, provider, model_version).embed_missing()  # type: ignore[arg-type]


def schedule_embedding_backfill(
    provider: LLMProvider,
    model_version: str,
    *,
    runner: Callable[[Callable[[], None]], None] | None = None,
) -> bool:
    """Ensure a single background backfill is embedding the missing-vector backlog.

    Returns True if this call started one, False if one was already running (deduped by the
    in-process lock, so concurrent read requests can't fan out N parallel backfills). ``runner``
    executes the work and defaults to a daemon thread; tests pass their own runner to avoid real
    threads and the separate DB connections they would open.
    """
    global _running
    with _lock:
        if _running:
            return False
        _running = True
    (runner or _spawn_daemon)(lambda: _run_backfill(provider, model_version))
    return True


def _spawn_daemon(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="embedding-backfill", daemon=True).start()


def _run_backfill(provider: LLMProvider, model_version: str) -> None:
    global _running
    try:
        from phare.db.base import get_session_factory

        with get_session_factory()() as session:
            embedded = run_embedding_backfill(session, provider, model_version)
            session.commit()
        logger.info("embeddings.backfill.done", extra={"embedded_count": embedded})
    except Exception:  # noqa: BLE001 - a backfill must never crash the process
        logger.exception("embeddings.backfill.failed")
    finally:
        with _lock:
            _running = False
