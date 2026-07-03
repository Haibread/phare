"""Secret redaction in logs: the TMDB api_key must never reach a log line in clear text."""

from __future__ import annotations

import json
import logging

from phare.core.logging import JsonFormatter, RedactSecretsFilter


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.makeLogRecord(
        {"name": "httpx", "levelno": logging.INFO, "msg": msg, "args": args or None}
    )


def test_filter_masks_api_key_in_message() -> None:
    record = _record(
        "HTTP Request: GET https://api.themoviedb.org/3/movie/popular?api_key=295f5secret"
    )
    assert RedactSecretsFilter().filter(record) is True
    masked = record.getMessage()
    assert "api_key=***" in masked
    assert "295f5secret" not in masked
    # The rest of the URL is kept intact for debugging.
    assert "api.themoviedb.org/3/movie/popular" in masked


def test_filter_masks_api_key_mid_query_string() -> None:
    record = _record("GET /3/search/movie?query=dune&api_key=deadbeef&language=en")
    RedactSecretsFilter().filter(record)
    masked = record.getMessage()
    assert "api_key=***" in masked
    assert "deadbeef" not in masked
    # Params after the key survive (only the value is masked).
    assert "language=en" in masked


def test_filter_survives_structured_json_formatting() -> None:
    # The redaction must hold through the JSON formatter, which reads getMessage().
    record = _record("GET https://api.themoviedb.org/3/movie/1?api_key=topsecret")
    RedactSecretsFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert "topsecret" not in payload["message"]
    assert "api_key=***" in payload["message"]


def test_filter_survives_lazy_args_interpolation() -> None:
    # httpx logs with %-style args; the secret must be masked even when it arrives via args.
    record = _record("HTTP Request: GET %s", "https://api.themoviedb.org/3/x?api_key=hush")
    RedactSecretsFilter().filter(record)
    masked = record.getMessage()
    assert "hush" not in masked
    assert "api_key=***" in masked


def test_filter_passes_through_when_no_secret() -> None:
    record = _record("nothing to hide here")
    assert RedactSecretsFilter().filter(record) is True
    assert record.getMessage() == "nothing to hide here"
