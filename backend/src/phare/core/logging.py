"""Structured logging, configured once at process start.

Emits one JSON object per line with the standard fields plus any structured key-values
passed via ``logger.<level>("msg", extra={...})``. Verbosity is controlled only by the
log level.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import Any

# LogRecord attributes that are not user-supplied structured fields.
_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "taskName"}


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
