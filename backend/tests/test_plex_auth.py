"""Plex auth provider: the pure parsing/URL helpers (no live Plex)."""

from __future__ import annotations

from phare.auth.plex import (
    build_auth_url,
    derive_client_identifier,
    parse_account,
    parse_server_ids,
)


def test_parse_account_prefers_uuid_then_falls_back() -> None:
    subject, name, email = parse_account(
        {"uuid": "abc-123", "id": 7, "title": "Theo", "username": "theo", "email": "t@x.test"}
    )
    assert subject == "abc-123"
    assert name == "Theo"
    assert email == "t@x.test"
    # No uuid -> numeric id; no title -> username; no email -> None.
    subject2, name2, email2 = parse_account({"id": 7, "username": "theo"})
    assert subject2 == "7"
    assert name2 == "theo"
    assert email2 is None


def test_parse_server_ids_keeps_only_servers() -> None:
    resources = [
        {"clientIdentifier": "srv-A", "provides": "server"},
        {"clientIdentifier": "srv-B", "provides": "server,player"},
        {"clientIdentifier": "client-1", "provides": "client,player"},  # not a server
        {"provides": "server"},  # no machine id -> skipped
    ]
    assert parse_server_ids(resources) == ("srv-A", "srv-B")


def test_build_auth_url_carries_client_and_code() -> None:
    url = build_auth_url("phare-abc", "PINCODE", "Phare")
    assert url.startswith("https://app.plex.tv/auth#?clientID=phare-abc")
    assert "code=PINCODE" in url


def test_client_identifier_is_stable_for_a_secret() -> None:
    assert derive_client_identifier("seed") == derive_client_identifier("seed")
    assert derive_client_identifier("seed") != derive_client_identifier("other")
