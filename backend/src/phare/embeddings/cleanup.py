"""Old-space cleanup — reclaim the superseded embedding vectors after a document cutover.

Each embedding space costs real storage (~4096 floats x ~7700 titles ~= 126 MB per space). Once
reads have flipped to the new document space (``active_embedding_version`` returns the write tag),
the previous space is dead weight and should be dropped. This runs in the background, off the read
path, once per cutover — the same daemon-thread pattern as the backfill.

Guards (never break the served space):
- Only delete when the *served* tag is the new write tag (the cutover has actually happened) — so
  we never delete the space that is currently answering queries.
- Never delete the served tag itself, and never delete a tag equal to it.
- The served tag is, by the cutover definition, already at full coverage — so removing the old tag
  can never leave the app with no servable space.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from phare.core.config import Settings
from phare.db.models import TitleEmbedding
from phare.embeddings.version import (
    active_embedding_version,
    embedding_model_version,
    embedding_write_version,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_running = False


def superseded_version(session: Session, settings: Settings) -> str | None:
    """The previous (doc-v1, bare) tag that is now safe to delete, or ``None`` if it isn't.

    Returns the superseded tag ONLY when the served tag is the write tag (the cutover has happened)
    and there are still vectors under the previous tag to reclaim. Otherwise ``None`` — reads are
    still being served from the previous space, or the write tag folds away, or there's nothing to
    delete. This is the single guard the scheduler and the delete both key on.
    """
    write = embedding_write_version(settings)
    previous = embedding_model_version(settings)
    if write == previous:
        return None  # no document suffix in play → no separate old space to clean
    if active_embedding_version(session, settings) != write:
        return None  # reads still served from the previous space — must not delete it
    stale = session.scalar(
        select(TitleEmbedding.title_id).where(TitleEmbedding.model_version == previous).limit(1)
    )
    return previous if stale is not None else None


def delete_superseded_embeddings(session: Session, settings: Settings) -> int:
    """Delete the superseded space's vectors if (and only if) it is safe to. Returns rows deleted.

    Re-checks :func:`superseded_version` itself (never trusts the caller) so it can't delete the
    served tag even under a race. The caller owns the commit.
    """
    victim = superseded_version(session, settings)
    if victim is None:
        return 0
    result = session.execute(delete(TitleEmbedding).where(TitleEmbedding.model_version == victim))
    deleted = result.rowcount or 0
    logger.info(
        "embeddings.space_cleanup",
        extra={
            "deleted_version": victim,
            "served_version": embedding_write_version(settings),
            "deleted_count": deleted,
        },
    )
    return deleted


def cleanup_running() -> bool:
    """True while a background cleanup is in flight (single-process, in-memory state)."""
    with _lock:
        return _running


def schedule_superseded_cleanup(
    settings: Settings,
    *,
    runner: Callable[[Callable[[], None]], None] | None = None,
) -> bool:
    """Ensure a single background cleanup runs when the previous space is safe to reclaim.

    No-ops (returns False) when there's nothing to clean or a cleanup is already running — deduped
    by the in-process lock so concurrent reads can't fan out parallel deletes. ``runner`` executes
    the work and defaults to a daemon thread; tests pass their own to avoid real threads."""
    global _running
    with _lock:
        if _running:
            return False
        _running = True
    (runner or _spawn_daemon)(lambda: _run_cleanup(settings))
    return True


def _spawn_daemon(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="embedding-cleanup", daemon=True).start()


def _run_cleanup(settings: Settings) -> None:
    global _running
    try:
        from phare.db.base import get_session_factory

        with get_session_factory()() as session:
            deleted = delete_superseded_embeddings(session, settings)
            session.commit()
        if deleted:
            logger.info("embeddings.space_cleanup.done", extra={"deleted_count": deleted})
    except Exception:  # noqa: BLE001 - a cleanup must never crash the process
        logger.exception("embeddings.space_cleanup.failed")
    finally:
        with _lock:
            _running = False
