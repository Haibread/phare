"""Language negotiation + the deterministic string catalog."""

from __future__ import annotations

import pytest

from phare.core.i18n import llm_output_directive, parse_accept_language, translate


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


def test_llm_output_directive_is_empty_for_english() -> None:
    # English is the source language of the prompts — no directive to append.
    assert llm_output_directive("en") == ""


def test_llm_output_directive_pins_french_vouvoiement() -> None:
    # R7: the LLM tutoied ("Tu adoreras…") while every static string vouvoies. The directive now
    # pins the polite form so the model's prose matches the canned strings, and still names French +
    # forbids an English opener.
    directive = llm_output_directive("fr")
    assert "French" in directive
    assert "vous" in directive and "tu" in directive  # instructs vous, forbids tu
    # It's appended to explanation/summary/composer prompts — an English request must add nothing.
    assert llm_output_directive("en") == ""
