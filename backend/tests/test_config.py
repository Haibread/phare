"""Settings parsing — guards the CORS_ORIGINS env handling."""

from __future__ import annotations

import pytest

from phare.core.config import Settings, get_settings


def test_suite_is_hermetic_no_real_llm() -> None:
    """The suite must never call a real LLM (cost/determinism). conftest blanks the credentials, so
    a developer's .env key cannot leak in. If this fails, the hermetic guard was removed."""
    assert not get_settings().llm_api_key


def test_suite_pins_closed_registration() -> None:
    """A dev's ``REGISTRATION_OPEN=true`` must not leak into tests that assert the closed-by-default
    posture; conftest pins it back to the secure default. If this fails, that pin was removed."""
    assert get_settings().registration_open is False


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
