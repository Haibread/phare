"""Language negotiation + the deterministic string catalog."""

from __future__ import annotations

import pytest

from phare.core.i18n import parse_accept_language, translate


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "en"),
        ("", "en"),
        ("fr", "fr"),
        ("FR", "fr"),
        ("fr-CA,fr;q=0.9,en;q=0.8", "fr"),  # browsers send weighted, region-tagged lists
        ("de,es", "en"),  # nothing supported -> default
        ("en-US", "en"),
    ],
)
def test_parse_accept_language(header: str | None, expected: str) -> None:
    assert parse_accept_language(header) == expected


def test_parse_accept_language_honours_custom_default() -> None:
    assert parse_accept_language(None, default="fr") == "fr"


def test_translate_picks_language_and_interpolates() -> None:
    assert translate("en", "row.youMightLike") == "You might like"
    assert translate("fr", "row.youMightLike") == "Pourrait vous plaire"
    assert (
        translate("fr", "row.becauseYouWatched", title="Heat") == "Parce que vous avez regardé Heat"
    )


def test_translate_falls_back_to_english_for_unknown_language() -> None:
    # A language not in the catalog entry resolves to the English source of truth.
    assert translate("de", "row.popular") == "Popular"  # type: ignore[arg-type]
