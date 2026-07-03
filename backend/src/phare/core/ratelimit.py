"""In-memory rate limiting (review I1).

A sliding-window counter guarded by a lock — no Redis, no ``slowapi``. The app is single-process
(one backend replica, in-process caches everywhere; see docs), so a per-process limiter is the
right size: it protects the expensive/abusable endpoints (login brute-force, the agent-model chat
turn, bulk imports) without adding a dependency.

The middleware is pure ASGI so it can reject *before* the app runs and never wraps the response —
important because the chat endpoint streams (SSE), and a response-buffering middleware would break
it. The applicable bucket is chosen by path; the key is the caller's user id when a bearer token is
present (parsed, not verified — a forged id just shares a bucket), else their IP.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from phare.core.auth import _parse_token
from phare.core.config import Settings
from phare.core.fallback import record_fallback


class SlidingWindowLimiter:
    """Thread-safe fixed-limit sliding window: at most ``limit`` hits per ``window`` seconds/key."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(
        self, key: str, limit: int, window: float, *, now: float | None = None
    ) -> tuple[bool, float]:
        """Record a hit and return ``(allowed, retry_after_seconds)``. When the window is full the
        hit is *not* recorded and ``retry_after`` says when the oldest hit ages out."""
        t = now if now is not None else time.time()
        with self._lock:
            q = self._hits[key]
            cutoff = t - window
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= limit:
                return False, max(0.0, q[0] + window - t)
            q.append(t)
            return True, 0.0


@dataclass(frozen=True)
class _Rule:
    name: str
    limit: int
    window: float
    per_user: bool  # True = key by user (fallback IP); False = key by IP only (auth endpoints)


def _rules(settings: Settings) -> dict[str, _Rule]:
    window = float(settings.rate_limit_window_seconds)
    out: dict[str, _Rule] = {}
    if window > 0 and settings.rate_limit_auth_per_window > 0:
        out["auth"] = _Rule("auth", settings.rate_limit_auth_per_window, window, per_user=False)
    if window > 0 and settings.rate_limit_chat_per_window > 0:
        out["chat"] = _Rule("chat", settings.rate_limit_chat_per_window, window, per_user=True)
    if window > 0 and settings.rate_limit_import_per_window > 0:
        out["import"] = _Rule(
            "import", settings.rate_limit_import_per_window, window, per_user=True
        )
    return out


# Credential endpoints worth throttling per IP. Deliberately excludes the Plex device-flow poll
# (/auth/plex/poll), which a client hits every couple of seconds by design, and /me.
_AUTH_PATHS = frozenset({"/auth/login", "/auth/register", "/auth/password"})


def _classify(path: str, method: str) -> str | None:
    """Which bucket (rule name) a request falls in, or ``None`` for an unlimited endpoint."""
    if path in _AUTH_PATHS or (
        path.startswith("/auth/admin/") and path.endswith("/reset-password")
    ):
        return "auth"
    if method == "POST" and "/chat" in path:
        return "chat"
    if path.startswith("/catalog/") and path != "/catalog/search":
        return "import"
    if path.startswith("/sources/") and path.endswith("/sync"):
        return "import"
    return None


def _client_ip(scope: dict) -> str:
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


def _caller_key(scope: dict, rule: _Rule) -> str:
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    if rule.per_user:
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        parsed = _parse_token(token) if token else None
        if parsed is not None:
            return f"{rule.name}:user:{parsed[0]}"
    return f"{rule.name}:ip:{_client_ip(scope)}"


class RateLimitMiddleware:
    """Reject over-quota requests with 429 before the app runs; pass everything else through."""

    def __init__(
        self, app, settings: Settings, limiter: SlidingWindowLimiter | None = None
    ) -> None:
        self.app = app
        self.settings = settings
        self._rules = _rules(settings)
        self.limiter = limiter or SlidingWindowLimiter()

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] != "http" or not self.settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return
        name = _classify(scope["path"], scope["method"])
        rule = self._rules.get(name) if name else None
        if rule is not None:
            allowed, retry = self.limiter.check(_caller_key(scope, rule), rule.limit, rule.window)
            if not allowed:
                record_fallback("rate_limit", name, path=scope["path"])
                await self._too_many(send, retry)
                return
        await self.app(scope, receive, send)

    async def _too_many(self, send, retry: float) -> None:
        retry_after = str(max(1, int(retry + 0.999)))
        body = b'{"detail":"Too many requests. Please slow down."}'
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", retry_after.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
