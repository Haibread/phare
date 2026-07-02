"""Canonical genre matching + the visible no-match fallback (review A2/A3, mission M1.2)."""

from __future__ import annotations

import logging
import uuid

from phare.agent.schema import ChatIntent
from phare.agent.service import intent_filter
from phare.recommend import genres
from phare.recommend.schema import Candidate


def _cand(*, title: str, genre_names: list[str]) -> Candidate:
    return Candidate(
        title_id=uuid.uuid4(),
        title=title,
        kind="movie",
        year=2020,
        genres=genre_names,
        keywords=[],
        runtime_minutes=120,
        popularity=None,
        overview=None,
        similarity=0.5,
    )


# --- the matching rule --------------------------------------------------------------------------


def test_scifi_matches_both_movie_and_tv_catalog_labels() -> None:
    # The planner emits "sci-fi"; the catalog says "Science Fiction" (movies) / "Sci-Fi & Fantasy"
    # (TV). Exact intersection matched neither (A2) — the alias + substring rule matches both.
    assert genres.term_matches("sci-fi", "Science Fiction")  # via alias
    assert genres.term_matches("sci-fi", "Sci-Fi & Fantasy")  # via substring


def test_french_genre_word_matches_english_catalog_label() -> None:
    assert genres.term_matches("horreur", "Horror")
    assert genres.term_matches("comédie", "Comedy")


def test_short_terms_require_equality_not_substring() -> None:
    # "war" (< 4 chars) must not match "wardrobe"; it still matches the genre "War" by equality.
    assert not genres.term_matches("war", "wardrobe")
    assert genres.term_matches("war", "War")


def test_unrelated_terms_do_not_match() -> None:
    assert not genres.term_matches("comedy", "Horror")


def test_in_vocabulary_covers_free_taste_keys() -> None:
    # The closed vocabulary the taste extractor draws from — genre names + tone descriptors.
    assert genres.in_vocabulary("Science Fiction")
    assert genres.in_vocabulary("Mind-bending Sci-Fi")  # resolves to the "mind-bending" descriptor
    assert not genres.in_vocabulary("Underwater Basket Weaving")


# --- the planner-path filter (this is the path A2 broke: it never normalized) -------------------


def test_planner_genre_filter_keeps_matching_titles() -> None:
    # Before M1.2 the planner path did a raw lowercase intersection, so "sci-fi" matched nothing and
    # the filter silently returned everything. Now it filters correctly.
    scifi_movie = _cand(title="Arrival", genre_names=["Science Fiction"])
    scifi_tv = _cand(title="Foundation", genre_names=["Sci-Fi & Fantasy"])
    comedy = _cand(title="Superbad", genre_names=["Comedy"])
    intent = ChatIntent(include_genres=["sci-fi"])
    kept = intent_filter(intent)([scifi_movie, scifi_tv, comedy])
    assert {c.title for c in kept} == {"Arrival", "Foundation"}


def test_genre_filter_no_match_falls_back_and_logs(caplog) -> None:
    # A genre the catalog doesn't carry: keep the full pool (thin-catalog safety) but SIGNAL it.
    only_comedy = _cand(title="Superbad", genre_names=["Comedy"])
    intent = ChatIntent(include_genres=["Documentary"])
    with caplog.at_level(logging.WARNING):
        kept = intent_filter(intent)([only_comedy])
    assert kept == [only_comedy]  # fallback: nothing dropped
    assert any(r.message == "genre_filter.no_match" for r in caplog.records)
