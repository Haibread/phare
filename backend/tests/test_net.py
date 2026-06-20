"""SSRF guard for user-supplied base URLs (core.net.validate_external_url)."""

from __future__ import annotations

import pytest

from phare.core.net import validate_external_url


def test_allows_public_and_lan_http_urls() -> None:
    # Self-hosters legitimately point at LAN addresses, so private ranges are allowed on purpose.
    for url in (
        "https://seerr.example.com",
        "http://plex.local:32400",
        "http://192.168.1.10:8096",
        "http://10.0.0.5",
    ):
        assert validate_external_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1:8000",  # loopback
        "http://localhost/admin",
        "http://0.0.0.0",
        "ftp://example.com",  # wrong scheme
        "file:///etc/passwd",
        "not-a-url",
    ],
)
def test_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_external_url(url)
