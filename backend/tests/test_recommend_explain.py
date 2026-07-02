"""Explanations: template fallback is spoiler-safe; LLM path is used when present."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from phare.db.models import Title, TitleKind
from phare.providers.fakes import FakeLLMProvider
from phare.providers.http import TTLCache
from phare.recommend.explain import (
    Explainer,
    PersistentReasonCache,
    _llm_prompt,
    _matched_affinity,
    _taste_fingerprint,
    coerce_safe,
    explain,
    is_spoiler_safe,
    stream_lazy_reason,
)
from phare.recommend.schema import Anchor, Recommendation

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


def test_template_names_the_matched_genre_once_not_twice() -> None:
    # Regression: the descriptor listed every genre AND the affinity clause repeated the matched
    # one, so a sci-fi pick read "Science Fiction, Mystery ... your taste for Science Fiction".
    taste = {"affinities": {"Science Fiction": 0.9}}
    [out] = explain([_rec(genres=["Science Fiction", "Mystery"])], taste, llm=None)
    assert out.explanation is not None
    assert out.explanation.count("Science Fiction") == 1  # named once, in the affinity clause
    assert "Mystery" in out.explanation  # the other genre still describes the title


def test_template_does_not_repeat_a_single_matched_genre() -> None:
    # When the only genre IS the matched one, name it once and use the generic profile tail rather
    # than "Comedy movie ... your taste for Comedy".
    taste = {"affinities": {"Comedy": 0.8}}
    [out] = explain([_rec(genres=["Comedy"])], taste, llm=None)
    assert out.explanation is not None
    assert out.explanation.count("Comedy") == 1


def test_swing_explanation_frames_it_as_a_stretch() -> None:
    [out] = explain([_rec(is_swing=True)], {}, llm=None)
    assert out.explanation is not None
    assert "discovery" in out.explanation.lower() or "stretch" in out.explanation.lower()


def test_template_is_localised_to_french() -> None:
    taste = {"affinities": {"Science Fiction": 0.9}}
    [out] = explain([_rec()], taste, llm=None, language="fr")
    assert out.explanation is not None
    assert out.explanation.startswith("Film")  # localised kind, not "A ... movie"
    assert "votre goût pour Science Fiction" in out.explanation  # localised affinity clause
    assert "2016" in out.explanation  # year still interpolated


def test_llm_used_when_available() -> None:
    llm = FakeLLMProvider(completion="A moody sci-fi that matches your taste.")
    [out] = explain([_rec()], {"summary": "x"}, llm=llm)
    assert out.explanation == "A moody sci-fi that matches your taste."
    assert llm.prompts  # the LLM was actually consulted


def test_llm_prompt_anchors_on_concrete_taste_hooks() -> None:
    # The prompt must hand the model specifics to latch onto — the shared genre affinity and the
    # viewer's stated likes — and demand a second-person sentence, so it can't return back-of-box
    # copy that mentions neither "you" nor anything the viewer actually likes.
    llm = FakeLLMProvider(completion="Right up your alley.")
    taste = {
        "summary": "loves cerebral sci-fi",
        "likes": ["slow-burn sci-fi", "moral ambiguity"],
        "affinities": {"Science Fiction": 0.9, "Comedy": -0.4},
    }
    explain([_rec()], taste, llm=llm)  # _rec genres include "Science Fiction"
    prompt = llm.prompts[0]
    assert "Science Fiction" in prompt  # the shared liked genre is surfaced as the anchor
    assert "slow-burn sci-fi" in prompt  # stated likes reach the model
    assert "as you" in prompt  # and it's told to address the viewer directly


def test_anchor_sharpens_the_prompt_and_gets_its_own_cache_bucket() -> None:
    # A card opened from a "because you watched Dune" row must (a) tell the model to open from that
    # link, and (b) cache separately from the same title's un-anchored / other-anchor reason.
    taste = {"summary": "loves epic sci-fi"}
    dune = Anchor(title_id=uuid.uuid4(), title="Dune", genres=["Science Fiction"])
    her = Anchor(title_id=uuid.uuid4(), title="Her", genres=["Drama", "Romance"])

    prompt = _llm_prompt(_rec(), taste, anchor=dune)
    assert "Dune" in prompt  # the seed title is named so the model can anchor on it
    assert "watched and loved" in prompt

    bare = _taste_fingerprint(taste)
    assert _taste_fingerprint(taste, dune) != bare  # anchored reason is a distinct cache entry...
    assert _taste_fingerprint(taste, dune) != _taste_fingerprint(taste, her)  # ...per anchor...
    assert _taste_fingerprint(taste) == bare  # ...while the un-anchored key is unchanged


def test_prompt_version_is_folded_into_the_cache_fingerprint() -> None:
    # A prompt-wording change must invalidate previously cached blurbs (in-process + Postgres),
    # otherwise a deploy keeps serving stale phrasing until taste happens to change. The version is
    # part of the fingerprint, so two versions of the same taste hash differently.
    import phare.recommend.explain as explain_mod

    taste = {"summary": "loves cerebral sci-fi"}
    before = explain_mod._taste_fingerprint(taste)
    monkey = "999-different"
    original = explain_mod._PROMPT_VERSION
    try:
        explain_mod._PROMPT_VERSION = monkey
        assert explain_mod._taste_fingerprint(taste) != before
    finally:
        explain_mod._PROMPT_VERSION = original


def test_matched_affinity_ignores_disliked_and_unshared_genres() -> None:
    rec = _rec(genres=["Comedy", "Drama"])
    # Comedy is disliked (negative), Drama isn't in affinities -> no positive shared genre.
    assert _matched_affinity(rec, {"affinities": {"Comedy": -0.5, "Science Fiction": 0.8}}) is None
    assert _matched_affinity(rec, {"affinities": {"Drama": 0.6}}) == "Drama"


def test_llm_prompt_carries_the_french_directive() -> None:
    llm = FakeLLMProvider(completion="Un thriller cérébral taillé pour vos goûts.")
    explain([_rec()], {"summary": "x"}, llm=llm, language="fr")
    assert "French" in llm.prompts[0]


def test_llm_prompt_has_no_directive_in_english() -> None:
    llm = FakeLLMProvider(completion="A moody sci-fi.")
    explain([_rec()], {"summary": "x"}, llm=llm)  # default English
    assert "French" not in llm.prompts[0]
    assert "Write your response in" not in llm.prompts[0]


def test_llm_failure_falls_back_to_template() -> None:
    class _BoomLLM:
        def complete(
            self, prompt: str, *, max_tokens: int | None = None, temperature: float | None = None
        ) -> str:
            raise RuntimeError("provider down")

        def embed(self, texts: object) -> object:  # pragma: no cover - unused
            raise NotImplementedError

    [out] = explain([_rec()], {}, llm=_BoomLLM())
    assert out.explanation is not None  # degraded, not crashed


def test_is_spoiler_safe_flags_plot_reveals_and_runaway_length() -> None:
    assert is_spoiler_safe("A moody, cerebral sci-fi that fits your love of slow-burn drama.")
    assert not is_spoiler_safe(_OVERVIEW_LEAK)  # contains "dies"
    assert not is_spoiler_safe("It's great because the killer is the brother — total plot twist.")
    assert not is_spoiler_safe("x " * 200)  # one-line blurbs aren't 400 chars long
    # Word-boundary, not substring: innocent words that merely contain a marker pass.
    assert is_spoiler_safe("A warm slate of comedies and studies in friendship.")


def test_llm_spoiler_output_falls_back_to_safe_template() -> None:
    llm = FakeLLMProvider(completion="You'll love it — the protagonist dies in the final act.")
    [out] = explain([_rec()], {"affinities": {"Science Fiction": 0.9}}, llm=llm)
    assert out.explanation is not None
    assert "dies" not in out.explanation  # the spoiler was rejected, template used instead
    assert "Science Fiction" in out.explanation


_FIRST_SENTENCE = "A taut, atmospheric thriller that rewards patience."


def test_coerce_safe_trims_overlong_marker_free_reply_to_first_sentence() -> None:
    long_reply = _FIRST_SENTENCE + " It also goes on with needless padding clauses." * 8
    assert len(long_reply) > 320  # a verbose run past the one-sentence cap...
    assert coerce_safe(long_reply) == _FIRST_SENTENCE  # ...is salvaged, not discarded
    assert coerce_safe("Gripping until the killer is unmasked.") is None  # a real marker rejects


def test_overlong_explanation_is_salvaged_not_templated() -> None:
    # Regression: a verbose-but-harmless reply used to be rejected and the template cached forever,
    # so on-taste top picks intermittently lost their LLM blurb. Now it's trimmed and kept.
    llm = FakeLLMProvider(completion=_FIRST_SENTENCE + " Plus assorted extra rambling." * 12)
    [out] = explain([_rec()], {"summary": "loves slow-burn thrillers"}, llm=llm)
    assert out.explanation == _FIRST_SENTENCE  # the LLM blurb survived, trimmed to one sentence


def test_explainer_spends_budget_on_top_ranked_items_first() -> None:
    # The budget is front-loaded onto the top-ranked items (the most visible); the tail templates.
    llm = FakeLLMProvider(completion="A great fit for your taste.")
    recs = [_rec(title=f"T{i}", title_id=uuid.uuid4()) for i in range(5)]
    out = Explainer(llm=llm, budget=2).explain(recs, {"summary": "s"})

    assert len(llm.prompts) == 2  # only two LLM calls (the budget)
    assert out[0].explanation == "A great fit for your taste."  # top picks get the blurb
    assert out[1].explanation == "A great fit for your taste."
    assert all(o.explanation != "A great fit for your taste." for o in out[2:])  # tail templated


def test_explanation_calls_are_token_capped() -> None:
    # A one-sentence blurb must ship a max_tokens cap so a chatty model can't run up the bill.
    llm = FakeLLMProvider(completion="A moody sci-fi that matches your taste.")
    explain([_rec()], {"summary": "x"}, llm=llm)
    assert llm.max_tokens and all(mt is not None for mt in llm.max_tokens)


def test_persistent_reason_cache_survives_a_cold_in_process_layer(db_session: Session) -> None:
    # The whole point of the DB tier: an accepted reason outlives the process, so a restart (an
    # empty L1) reads it back from Postgres instead of paying the model again.
    title = Title(kind=TitleKind.movie, title="Heat", year=1995)
    db_session.add(title)
    db_session.flush()
    key = ("reason", str(title.id), "abcd1234abcd1234")

    PersistentReasonCache(db_session, TTLCache(ttl=3600)).set(key, "A taut crime epic.")
    cold = PersistentReasonCache(db_session, TTLCache(ttl=3600))  # fresh L1, like a new process
    assert cold.get(key) == "A taut crime epic."


def test_lazy_reason_persists_and_then_replays_without_the_model(db_session: Session) -> None:
    title = Title(kind=TitleKind.movie, title="Sicario", year=2015, genres=["Thriller"])
    db_session.add(title)
    db_session.flush()
    rec = Recommendation(
        title_id=title.id, title="Sicario", kind="movie", year=2015, genres=["Thriller"], score=0.8
    )
    taste = {"summary": "loves tense thrillers"}
    llm = FakeLLMProvider(completion="A tense, methodical thriller right up your alley.")

    cache = PersistentReasonCache(db_session, TTLCache(ttl=3600))
    first = "".join(stream_lazy_reason(rec, taste, llm, cache))
    assert first == "A tense, methodical thriller right up your alley."
    assert len(llm.prompts) == 1

    # A new process (cold L1) re-opens the same card: served from Postgres, no second model call.
    cold = FakeLLMProvider(completion="DIFFERENT — should not be generated")
    replay = "".join(
        stream_lazy_reason(rec, taste, cold, PersistentReasonCache(db_session, TTLCache(ttl=3600)))
    )
    assert replay == first
    assert cold.prompts == []  # the durable cache spared the workhorse call


# --- G2: a transient LLM error must not cache the template forever (mission M2.4) --------------


class _FlakyThenGoodLLM:
    """Raises on the first complete() call, succeeds after — a recovered provider."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, max_tokens: int | None = None, temperature=None) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("provider down")
        return "Since you like cerebral sci-fi, this one lingers."

    def embed(self, texts):  # pragma: no cover - Explainer never embeds
        return [[0.0] * 1536 for _ in texts]


class _SpoilerLLM:
    """Always narrates plot, so every completion is rejected by the spoiler guard."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, max_tokens: int | None = None, temperature=None) -> str:
        self.calls += 1
        return "You'll love how the killer is revealed at the very end."

    def embed(self, texts):  # pragma: no cover
        return [[0.0] * 1536 for _ in texts]


def test_transient_llm_error_is_not_cached_and_retries() -> None:
    cache = TTLCache(ttl=9999, maxsize=16)
    llm = _FlakyThenGoodLLM()
    rec = _rec()
    taste = {"summary": "cerebral sci-fi", "affinities": {"Science Fiction": 0.9}}

    Explainer(llm=llm, cache=cache).explain([rec], taste)  # errors → template, NOT cached
    second = Explainer(llm=llm, cache=cache).explain([rec], taste)  # retries → real blurb

    assert llm.calls == 2  # the error didn't get cached, so the next render re-attempted
    assert second[0].explanation and "cerebral" in second[0].explanation.lower()


def test_spoiler_rejection_is_cached_and_not_retried() -> None:
    cache = TTLCache(ttl=9999, maxsize=16)
    llm = _SpoilerLLM()
    rec = _rec()
    taste = {"summary": "x"}

    Explainer(llm=llm, cache=cache).explain([rec], taste)  # rejected → safe template, cached
    Explainer(llm=llm, cache=cache).explain([rec], taste)  # cache hit → no second call

    assert llm.calls == 1  # a reliable spoiler is a stable outcome; don't re-burn budget on it
