"""Tolerant JSON extraction from LLM completions (reasoning blocks, fences, prose, empties)."""

from __future__ import annotations

import pytest

from phare.llm_json import extract_json


def test_parses_plain_object() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_parses_plain_array() -> None:
    assert extract_json('[{"title": "x"}]') == [{"title": "x"}]


def test_strips_markdown_fence() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_strips_reasoning_block() -> None:
    raw = '<think>The user likes sci-fi, so…</think>\n{"calls": []}'
    assert extract_json(raw) == {"calls": []}


def test_salvages_json_wrapped_in_prose() -> None:
    assert extract_json('Sure! Here it is: {"a": 1} — hope that helps.') == {"a": 1}


@pytest.mark.parametrize("raw", [None, "", "   ", "<think>only thinking, no answer</think>"])
def test_raises_on_nothing_parseable(raw: str | None) -> None:
    with pytest.raises(ValueError):
        extract_json(raw)


def test_raises_when_no_json_present() -> None:
    with pytest.raises(ValueError):
        extract_json("I can't help with that.")
