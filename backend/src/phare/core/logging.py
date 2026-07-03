"""Structured logging, configured once at process start.

Emits one JSON object per line with the standard fields plus any structured key-values
passed via ``logger.<level>("msg", extra={...})``. Verbosity is controlled only by the
log level.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
from typing import Any

# LogRecord attributes that are not user-supplied structured fields.
_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "taskName"}

# TMDB v3 auth rides in the query string, and httpx logs every request URL at INFO — so an
# unredacted line leaks the key in clear text. Mask the value of an ``api_key`` query param wherever
# it appears in a formatted message, keeping the rest of the URL intact for debugging.
_API_KEY_RE = re.compile(r"(api_key=)[^&\s\"']+", re.IGNORECASE)


def _redact(text: str) -> str:
    """Replace any ``api_key=<value>`` with ``api_key=***``."""
    return _API_KEY_RE.sub(r"\1***", text)


class RedactSecretsFilter(logging.Filter):
    """Masks secrets (currently the TMDB ``api_key`` query param) in a record's message.

    Installed on the ``httpx`` logger, whose INFO request lines carry the full URL. Rewrites the
    already-interpolated message so the redaction survives both plain and structured (JSON)
    formatting — a formatter reads ``record.getMessage()``, which we've pinned to the masked text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _redact(message)
        if redacted != message:
            # Pin the masked text and clear args so getMessage() can't re-interpolate the secret.
            record.msg = redacted
            record.args = None
        return True


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON line, including ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    """Install the JSON handler on the root logger. Idempotent."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Scrub the TMDB api_key out of httpx's request-URL logs before they reach any handler. The
    # filter runs at the originating logger, so the masked record is what propagates to root.
    _install_redaction()


def _install_redaction() -> None:
    """Attach the secret-redaction filter to the ``httpx`` logger, at most once."""
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, RedactSecretsFilter) for f in httpx_logger.filters):
        httpx_logger.addFilter(RedactSecretsFilter())
