"""Shared HTTP plumbing for external providers: bounded 429 retry + a tiny TTL cache.

Every outbound provider call can hit a rate limit. Rather than re-implement backoff per provider
(it lived only in Trakt before), they share :func:`request_with_retry`, which honours
``Retry-After`` with a bounded number of attempts.

Idempotent third-party reads (TMDB metadata/search) also share :class:`TTLCache` so repeat
lookups within a window — e.g. the chat agent resolving the same title across turns — don't
re-hit the API. Self-hosted sources (Plex/Jellyfin/Seerr) get the retry but not the cache: they
won't rate-limit their own owner, and their state is mutable, so a stale read would be worse than
a fresh call.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_MAX_RETRIES = 3
_RETRY_AFTER_CAP = 60.0
_RETRY_STATUSES = frozenset({429})


def retry_after_seconds(
    response: httpx.Response, *, default: float = 1.0, cap: float = _RETRY_AFTER_CAP
) -> float:
    """Seconds to wait before retrying, honouring a numeric ``Retry-After`` header (clamped)."""
    raw = response.headers.get("Retry-After", "").strip()
    seconds = float(raw) if raw.replace(".", "", 1).isdigit() else default
    return min(max(seconds, 0.0), cap)


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    name: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
    retry_statuses: frozenset[int] = _RETRY_STATUSES,
    **kwargs: Any,
) -> httpx.Response:
    """Issue a request, retrying on rate-limit status codes with ``Retry-After`` backoff.

    Returns the final response **without** raising for status — callers decide what non-retried
    codes mean (e.g. Seerr treats 404 as "requestable"). Only ``retry_statuses`` (429 by default)
    trigger a retry; everything else returns immediately.
    """
    for attempt in range(max_retries + 1):
        response = client.request(method, url, **kwargs)
        if response.status_code in retry_statuses and attempt < max_retries:
            wait = retry_after_seconds(response)
            logger.warning(
                "%s.rate_limited",
                name,
                extra={"url": url, "retry_after": wait, "attempt": attempt + 1},
            )
            sleep(wait)
            continue
        return response
    raise RuntimeError("unreachable")  # pragma: no cover - loop always returns


class TTLCache:
    """A tiny thread-safe in-process TTL + LRU cache. Single-user self-hosted scale — no Redis.

    ``ttl <= 0`` disables caching (every :meth:`get_or_set` calls the factory). ``maxsize`` bounds
    memory via LRU eviction so a long-running process can't grow without limit. The factory runs
    outside the lock, so a cold race may fetch twice — harmless for idempotent reads.
    """

    def __init__(
        self, *, ttl: float, maxsize: int = 1024, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl
        self._maxsize = maxsize
        self._clock = clock
        self._lock = threading.Lock()
        self._store: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()

    def get(self, key: Hashable) -> Any | None:
        """Return the cached value if present and unexpired, else ``None`` (evicting if stale)."""
        if self._ttl <= 0:
            return None
        now = self._clock()
        with self._lock:
            hit = self._store.get(key)
            if hit is None:
                return None
            expires_at, value = hit
            if expires_at > now:
                self._store.move_to_end(key)
                return value
            del self._store[key]
            return None

    def set(self, key: Hashable, value: Any) -> None:
        """Store ``value`` under ``key`` with the configured TTL (no-op when caching off)."""
        if self._ttl <= 0:
            return
        with self._lock:
            self._store[key] = (self._clock() + self._ttl, value)
            self._store.move_to_end(key)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def get_or_set(self, key: Hashable, factory: Callable[[], T]) -> T:
        if self._ttl <= 0:
            return factory()
        cached = self.get(key)
        if cached is not None:
            return cached
        value = factory()
        self.set(key, value)
        return value
