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


def test_french_localized_vocabulary_still_steers_against_english_catalog() -> None:
    # R7: taste chips now emit in the profile's language, so a French genre key drawn from the
    # localised controlled vocabulary must still match the catalog's English TMDB labels. Cover the
    # forms that only got aliases in R7 (the TV-flavoured / compound genres) plus a compound chip.
    assert genres.matches_any(["Enfants"], ["Kids"])
    assert genres.matches_any(["Téléréalité"], ["Reality"])
    assert genres.matches_any(["Science-Fiction & Fantastique"], ["Sci-Fi & Fantasy"])
    assert genres.matches_any(["Guerre & Politique"], ["War & Politics"])
    assert genres.matches_any(["Action & Aventure"], ["Action & Adventure"])
    # A compound genre-level chip ("science-fiction cérébrale") still steers via the substring rule.
    assert genres.matches_any(["science-fiction cérébrale"], ["Science Fiction"])
    # And each localised vocabulary key resolves in-vocabulary, so it isn't flagged as inert.
    assert genres.in_vocabulary("Enfants")
    assert genres.in_vocabulary("Téléréalité")


def test_anime_alias_resolves_to_animation() -> None:
    # First-step anime handling (round 8, item 4): "anime" and its French/accented forms resolve to
    # the Animation catalog label. True anime (Animation + Japanese origin) is a later round.
    for word in ("anime", "animé", "animés", "animes"):
        assert genres.canonical(word) == "animation"
        assert genres.term_matches(word, "Animation")
    assert genres.matches_any(["anime"], ["Animation"])


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
    # Emitted via the shared fallback convention (G1): "<component>.fallback" + a reason field.
    assert any(
        r.message == "genre_filter.fallback" and r.reason == "no_match" for r in caplog.records
    )


def test_translate_genre_localizes_and_falls_back(caplog) -> None:
    # F3: stored English genre labels are display-translated for FR; unknown names fall back to
    # English and emit a fallback signal so a sentence never shows a blank.
    assert genres.translate_genre("Science Fiction", "fr") == "Science-Fiction"
    assert genres.translate_genre("Horror", "fr") == "Horreur"
    # English is the stored language — no translation applied.
    assert genres.translate_genre("Science Fiction", "en") == "Science Fiction"
    assert genres.translate_genres(["Drama", "Crime"], "fr") == ["Drame", "Crime"]
    with caplog.at_level(logging.WARNING):
        assert (
            genres.translate_genre("Underwater Basket Weaving", "fr") == "Underwater Basket Weaving"
        )
    assert any(
        r.message == "genre_translation.fallback" and r.reason == "unmapped" for r in caplog.records
    )


def test_resolve_catalog_genres_maps_intent_words_to_stored_labels() -> None:
    # The SQL re-fetch needs the *literal* catalog labels to overlap against. Loose intent words
    # (aliases, casing, a substring) resolve to the exact stored labels via the shared match rule;
    # a word matching no catalog genre resolves to nothing (the caller then skips the SQL filter).
    catalog = ["Comedy", "Science Fiction", "Thriller", "Horror"]
    assert genres.resolve_catalog_genres(["comedy"], catalog) == ["Comedy"]
    assert genres.resolve_catalog_genres(["comédie"], catalog) == ["Comedy"]  # FR alias
    assert genres.resolve_catalog_genres(["sci-fi"], catalog) == ["Science Fiction"]  # alias
    assert genres.resolve_catalog_genres(["western"], catalog) == []  # not in this catalog
    # Multiple wants dedup to the union of matched labels, in stable (sorted) order.
    assert genres.resolve_catalog_genres(["comedy", "horror"], catalog) == ["Comedy", "Horror"]
