"""Per-title metadata heal — the shared primitive behind the read-path runtime enrichment and the
boot-time bulk runtime backfill.

The broad TMDB *discover* import omits runtime (and, historically, an insert bug dropped
``vote_average``), so most catalog rows land with ``runtime_minutes`` / ``vote_average`` /
``vote_count`` NULL. Both healing paths do the same two things — fetch a title's TMDB detail and
copy the missing fields onto the row without ever clobbering a value that's already there — so that
logic lives here once (rule of three: read-path :meth:`RecommendationService._enrich_runtimes`, the
bulk boot backfill, and any future caller share it).

Idempotent by construction: every write is guarded by ``is None``, so re-running only ever fills
holes. Best-effort per title: a flaky fetch is logged and skipped, never sinks the batch.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from phare.db.models import Title
from phare.providers.types import MetadataProvider, TitleMetadata

logger = logging.getLogger(__name__)

# Bounded concurrency for the per-title TMDB detail fan-out. httpx is thread-safe and the worker
# threads do *only* HTTP (never touch the session), so this parallelises the network wait without
# racing the DB. Shared by both heal paths so they can't drift apart.
RUNTIME_FETCH_WORKERS = 8


def apply_metadata_heal(row: Title, meta: TitleMetadata) -> bool:
    """Copy runtime / vote fields from ``meta`` onto ``row`` where the row is missing them.

    Never clobbers an existing value (idempotent — only fills NULLs). Returns True when the runtime
    was filled (the signal the runtime-cap filter and the gap gauge care about); vote heals don't
    flip it. The caller owns the commit.
    """
    filled_runtime = False
    if row.runtime_minutes is None and meta.runtime_minutes is not None:
        row.runtime_minutes = meta.runtime_minutes
        filled_runtime = True
    if row.vote_average is None and meta.vote_average is not None:
        row.vote_average = meta.vote_average
    if row.vote_count is None and meta.vote_count is not None:
        row.vote_count = meta.vote_count
    return filled_runtime


def fetch_metadata_parallel(
    source: MetadataProvider, rows: Iterable[Title]
) -> dict[uuid.UUID, TitleMetadata]:
    """Fetch TMDB detail for each row with a ``tmdb_id``, in parallel (bounded concurrency).

    Returns ``{title_id: metadata}`` for the fetches that succeeded — a failed or empty fetch is
    logged and simply absent. The worker threads only do HTTP; the caller applies the results to
    session-bound rows on the main thread (see :func:`apply_metadata_heal`).
    """
    fetchable = [(row.id, row.tmdb_id, row.kind) for row in rows if row.tmdb_id is not None]
    if not fetchable:
        return {}

    def fetch(item: tuple[uuid.UUID, int, object]) -> tuple[uuid.UUID, TitleMetadata | None]:
        title_id, tmdb_id, kind = item
        try:
            meta = source.get_title(tmdb_id, kind)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - a flaky fetch must not sink the batch
            logger.warning("catalog.heal.fetch_failed", extra={"title_id": str(title_id)})
            return title_id, None
        return title_id, meta

    out: dict[uuid.UUID, TitleMetadata] = {}
    with ThreadPoolExecutor(max_workers=RUNTIME_FETCH_WORKERS) as pool:
        for title_id, meta in pool.map(fetch, fetchable):
            if meta is not None:
                out[title_id] = meta
    return out
