"""Explanations: template fallback is spoiler-safe; LLM path is used when present."""

from __future__ import annotations

import uuid

from phare.providers.fakes import FakeLLMProvider
from phare.recommend.explain import explain
from phare.recommend.schema import Recommendation

_OVERVIEW_LEAK = "the protagonist secretly dies at the end"


def _rec(**kw: object) -> Recommendation:
    base: dict[str, object] = dict(
        title_id=uuid.uuid4(),
        title="Arrival",
        kind="movie",
        year=2016,
        genres=["Science Fiction", "Drama"],
        score=0.9,
    )
    base.update(kw)
    return Recommendation(**base)


def test_template_names_genre_and_year_without_leaking_plot() -> None:
    taste = {"affinities": {"Science Fiction": 0.9}, "summary": "loves cerebral sci-fi"}
    [out] = explain([_rec()], taste, llm=None)
    assert out.explanation is not None
    assert "2016" in out.explanation
    assert "Science Fiction" in out.explanation
    # The template only uses structured metadata, so it can never echo overview/plot text.
    assert _OVERVIEW_LEAK not in out.explanation


def test_swing_explanation_frames_it_as_a_stretch() -> None:
    [out] = explain([_rec(is_swing=True)], {}, llm=None)
    assert out.explanation is not None
    assert "discovery" in out.explanation.lower() or "stretch" in out.explanation.lower()


def test_llm_used_when_available() -> None:
    llm = FakeLLMProvider(completion="A moody sci-fi that matches your taste.")
    [out] = explain([_rec()], {"summary": "x"}, llm=llm)
    assert out.explanation == "A moody sci-fi that matches your taste."
    assert llm.prompts  # the LLM was actually consulted


def test_llm_failure_falls_back_to_template() -> None:
    class _BoomLLM:
        def complete(self, prompt: str) -> str:
            raise RuntimeError("provider down")

        def embed(self, texts: object) -> object:  # pragma: no cover - unused
            raise NotImplementedError

    [out] = explain([_rec()], {}, llm=_BoomLLM())
    assert out.explanation is not None  # degraded, not crashed
