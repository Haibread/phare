"""Canonical genre matching + the visible no-match fallback (review A2/A3, mission M1.2)."""

from __future__ import annotations

import logging
import uuid

from phare.agent.schema import ChatIntent
from phare.agent.service import intent_filter
from phare.recommend import genres
from phare.recommend.schema import Candidate


def _cand(*, title: str, genre_names: list[str], language: str | None = None) -> Candidate:
    return Candidate(
        title_id=uuid.uuid4(),
        title=title,
        kind="movie",
        year=2020,
        genres=genre_names,
        keywords=[],
        runtime_minutes=120,
        popularity=None,
        original_language=language,
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
    # "anime" and its French/accented forms resolve to the Animation catalog label (the genre half
    # of the constraint); the origin half (ja) is tested below.
    for word in ("anime", "animé", "animés", "animes"):
        assert genres.canonical(word) == "animation"
        assert genres.term_matches(word, "Animation")
        assert genres.origin_language_for(word) == "ja"  # ...and each is origin-scoped to Japan
    assert genres.matches_any(["anime"], ["Animation"])
    assert genres.origin_language_for("animation") is None  # plain animation is NOT origin-scoped


# --- origin-scoped constraints ("anime" = Animation + ja) ----------------------------------------


def test_resolve_catalog_constraints_scopes_anime_to_japanese_origin() -> None:
    catalog = ["Animation", "Comedy", "Drama"]
    constraints = genres.resolve_catalog_constraints(["anime"], catalog)
    assert constraints == [genres.GenreConstraint(labels=("Animation",), original_language="ja")]


def test_resolve_catalog_constraints_plain_animation_carries_no_language() -> None:
    catalog = ["Animation", "Comedy"]
    constraints = genres.resolve_catalog_constraints(["animation"], catalog)
    assert constraints == [genres.GenreConstraint(labels=("Animation",))]
    assert constraints[0].original_language is None


def test_resolve_catalog_constraints_mixes_plain_and_scoped_as_separate_or_terms() -> None:
    # "anime or comedy": the ja condition binds only to the anime constraint — a Japanese comedy
    # must not be demanded (OR semantics, matching the in-memory filter).
    catalog = ["Animation", "Comedy"]
    constraints = genres.resolve_catalog_constraints(["anime", "comedy"], catalog)
    assert genres.GenreConstraint(labels=("Comedy",)) in constraints
    assert genres.GenreConstraint(labels=("Animation",), original_language="ja") in constraints


def test_resolve_catalog_constraints_downgrades_anime_without_coverage_and_records(
    caplog,
) -> None:
    # Pre-heal catalog (0% original_language): the ja condition would exclude everything, so anime
    # downgrades to plain Animation — visibly, never silently.
    catalog = ["Animation"]
    with caplog.at_level(logging.WARNING, logger="phare.fallback"):
        constraints = genres.resolve_catalog_constraints(
            ["anime"], catalog, language_coverage=False
        )
    assert constraints == [genres.GenreConstraint(labels=("Animation",))]
    assert any(
        r.name == "phare.fallback" and getattr(r, "reason", "") == "anime_language_unknown"
        for r in caplog.records
    )


def test_intent_filter_anime_keeps_only_japanese_animation() -> None:
    # The coordinator's live case: a Batman-heavy profile asking for anime got DC animated movies.
    # With language data present, "anime" must keep Animation + ja only — western animation and a
    # Japanese non-animation title both fail.
    ja_anime = _cand(title="Akira", genre_names=["Animation"], language="ja")
    western = _cand(title="Batman: Mask of the Phantasm", genre_names=["Animation"], language="en")
    ja_drama = _cand(title="Shoplifters", genre_names=["Drama"], language="ja")
    unhealed = _cand(title="Old Cartoon", genre_names=["Animation"], language=None)
    kept = intent_filter(ChatIntent(include_genres=["anime"]))(
        [ja_anime, western, ja_drama, unhealed]
    )
    # NULL-language animation is excluded too (coverage exists → honest thin slate, no guessing).
    assert [c.title for c in kept] == ["Akira"]


def test_intent_filter_plain_animation_ignores_language() -> None:
    # "animation" (and "dessin animé" per the mood map) stays a plain genre: all Animation passes.
    ja_anime = _cand(title="Akira", genre_names=["Animation"], language="ja")
    western = _cand(title="Batman: Mask of the Phantasm", genre_names=["Animation"], language="en")
    drama = _cand(title="Heat", genre_names=["Drama"], language="en")
    kept = intent_filter(ChatIntent(include_genres=["animation"]))([ja_anime, western, drama])
    assert {c.title for c in kept} == {"Akira", "Batman: Mask of the Phantasm"}


def test_intent_filter_anime_downgrades_on_a_preheal_pool_and_records(caplog) -> None:
    # Pre-heal: no candidate carries a language at all, so the origin condition is unenforceable —
    # anime downgrades to plain Animation (stays useful) and the downgrade is recorded.
    western = _cand(title="Batman: Mask of the Phantasm", genre_names=["Animation"], language=None)
    drama = _cand(title="Heat", genre_names=["Drama"], language=None)
    with caplog.at_level(logging.WARNING, logger="phare.fallback"):
        kept = intent_filter(ChatIntent(include_genres=["anime"]))([western, drama])
    assert [c.title for c in kept] == ["Batman: Mask of the Phantasm"]
    assert any(
        r.name == "phare.fallback" and getattr(r, "reason", "") == "anime_language_unknown"
        for r in caplog.records
    )


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
