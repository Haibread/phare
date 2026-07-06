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
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

from phare.db.models import Title
from phare.providers.types import MetadataProvider, TitleMetadata

logger = logging.getLogger(__name__)

# Bounded concurrency for the per-title TMDB detail fan-out. httpx is thread-safe and the worker
# threads do *only* HTTP (never touch the session), so this parallelises the network wait without
# racing the DB. Shared by both heal paths so they can't drift apart.
RUNTIME_FETCH_WORKERS = 8


class RateLimiter:
    """A dead-simple thread-safe token bucket — one ``acquire()`` blocks until a token is free.

    Bounds the *bulk* metadata walker's request rate against TMDB (their guidance is ~50 rps; the
    8-worker fan-out on a warm CDN measured ~176 rps live, enough to trip a 429 storm that the
    keyset cursor would then skip whole batches over until the next boot). Wall-clock doesn't matter
    for a background job, so a plain steady drip is fine. The read path (chat ``_enrich_runtimes``,
    ≤64 fetches) is deliberately *not* throttled — it passes no limiter.

    ``clock``/``sleep`` are injectable so tests drive it on a fake clock with no real waiting.
    """

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        """Block until the next token is due, then reserve the following slot (steady drip)."""
        if self._interval <= 0.0:
            return
        with self._lock:
            now = self._clock()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._interval
        if wait > 0.0:
            self._sleep(wait)


def apply_metadata_heal(row: Title, meta: TitleMetadata) -> bool:
    """Copy missing metadata (runtime, votes, credits, original language) from ``meta`` to ``row``.

    Never clobbers an existing value (idempotent — only fills holes: a NULL scalar or an *empty*
    credit array). Returns True when *anything* was filled — the signal the gap gauge and the caller
    use to count progress; a title whose runtime was already present but whose credits/language were
    healed still counts. The caller owns the commit.
    """
    filled = False
    if row.runtime_minutes is None and meta.runtime_minutes is not None:
        row.runtime_minutes = meta.runtime_minutes
        filled = True
    if row.vote_average is None and meta.vote_average is not None:
        row.vote_average = meta.vote_average
    if row.vote_count is None and meta.vote_count is not None:
        row.vote_count = meta.vote_count
    # Credits are empty-by-default arrays, so "missing" is empty, not NULL. Fill only when we have
    # something to write, so an unfillable fetch doesn't flip the progress signal for nothing.
    if not row.directors and meta.directors:
        row.directors = list(meta.directors)
        filled = True
    if not row.top_cast and meta.top_cast:
        row.top_cast = list(meta.top_cast)
        filled = True
    if row.original_language is None and meta.original_language is not None:
        row.original_language = meta.original_language
        filled = True
    return filled


def fetch_metadata_parallel(
    source: MetadataProvider,
    rows: Iterable[Title],
    *,
    limiter: RateLimiter | None = None,
) -> dict[uuid.UUID, TitleMetadata]:
    """Fetch TMDB detail for each row with a ``tmdb_id``, in parallel (bounded concurrency).

    Returns ``{title_id: metadata}`` for the fetches that succeeded — a failed or empty fetch is
    logged and simply absent. The worker threads only do HTTP; the caller applies the results to
    session-bound rows on the main thread (see :func:`apply_metadata_heal`).

    ``limiter`` (optional) caps the request rate before each fetch — passed by the bulk walker to
    stay well under TMDB's rps guidance; the read path omits it so chat turns aren't slowed. A 429
    still triggers the provider's own ``Retry-After`` retry inside ``get_title``.
    """
    fetchable = [(row.id, row.tmdb_id, row.kind) for row in rows if row.tmdb_id is not None]
    if not fetchable:
        return {}

    def fetch(item: tuple[uuid.UUID, int, object]) -> tuple[uuid.UUID, TitleMetadata | None]:
        title_id, tmdb_id, kind = item
        if limiter is not None:
            limiter.acquire()
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
