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


def test_suite_disables_env_file_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repo-root ``.env`` must not leak into the suite even for a var no test set. conftest
    points ``Settings.model_config.env_file`` at nothing, so pydantic-settings never falls back to
    the file — a var absent from the environment reads as unset, not as the developer's live value.

    Guards the hermeticity hole from a real ``.env`` (live Trakt/LLM creds): a test that ``delenv``s
    a credential used to re-expose the file's value and hit the network. If this assertion fails,
    that guard was removed and the suite is no longer hermetic against ``.env``.
    """
    assert Settings.model_config["env_file"] is None
    # And it actually takes effect: a fresh Settings with a credential deleted from the environment
    # comes back unset, not populated from the repo .env.
    monkeypatch.delenv("TRAKT_CLIENT_ID", raising=False)
    assert Settings().trakt_client_id is None


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
