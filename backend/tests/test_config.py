"""Settings parsing — guards the CORS_ORIGINS env handling."""

from __future__ import annotations

import pytest

from phare.core.config import Settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:5173", ["http://localhost:5173"]),
        ("http://a.test, http://b.test", ["http://a.test", "http://b.test"]),
        ("", []),
        ('["http://json.test"]', ["http://json.test"]),
    ],
)
def test_cors_origins_parses_from_env(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    monkeypatch.setenv("CORS_ORIGINS", raw)
    settings = Settings(_env_file=None)
    assert settings.cors_origins == expected
