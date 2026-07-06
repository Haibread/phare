"""Catalog seeding (offline sample) + TMDB import upsert behaviour."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from phare.api.catalog import ensure_import_allowed
from phare.catalog.sample import seed_sample_catalog
from phare.catalog.service import (
    broad_import_from_tmdb,
    import_from_tmdb,
    search_titles,
    upsert_titles,
)
from phare.cli import app as cli_app
from phare.core.config import Settings, get_settings
from phare.db.models import EMBEDDING_DIM, Title, TitleEmbedding, TitleKind
from phare.providers.embeddings_local import LOCAL_MODEL_VERSION
from phare.providers.types import TitleMetadata
from tests.conftest import authed_client, make_account


def _count_titles(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Title)) or 0


def test_sample_catalog_titles_carry_real_posters(db_session: Session) -> None:
    # The sample-data path is a new user's first impression; every sample title must ship a real
    # TMDB poster so it isn't a wall of text blocks (works offline — the image CDN needs no key).
    seed_sample_catalog(db_session)
    posters = db_session.scalars(select(Title.poster_path)).all()
    assert posters and all(p and p.startswith("/") and p.endswith(".jpg") for p in posters)


def test_seed_sample_catalog_is_idempotent(db_session: Session) -> None:
    created = seed_sample_catalog(db_session)
    assert created > 20  # a diverse pool, not a couple of titles
    total_after_first = _count_titles(db_session)

    created_again = seed_sample_catalog(db_session)
    assert created_again == 0  # nothing new on a re-seed
    assert _count_titles(db_session) == total_after_first


def test_sample_catalog_spans_movies_and_shows(db_session: Session) -> None:
    seed_sample_catalog(db_session)
    kinds = set(db_session.scalars(select(Title.kind)).all())
    assert kinds == {TitleKind.movie, TitleKind.show}


def test_upsert_refreshes_popularity_without_duplicating(db_session: Session) -> None:
    meta = TitleMetadata(kind=TitleKind.movie, tmdb_id=999001, title="X", popularity=1.0)
    assert upsert_titles(db_session, [meta]) == 1

    updated = meta.model_copy(update={"popularity": 9.0})
    assert upsert_titles(db_session, [updated]) == 0  # no new row
    row = db_session.scalar(select(Title).where(Title.tmdb_id == 999001))
    assert row is not None and row.popularity == 9.0


def test_upsert_persists_vote_average_on_insert(db_session: Session) -> None:
    # A freshly-inserted title must carry vote_average, not just vote_count/popularity — the insert
    # branch used to omit it, leaving ~98% of a broad-imported catalog with NULL vote_average, which
    # silently disabled the re-ranker's quality floor (a poorly-rated title took no penalty).
    meta = TitleMetadata(
        kind=TitleKind.movie,
        tmdb_id=999002,
        title="Rated",
        popularity=5.0,
        vote_count=1_200,
        vote_average=7.4,
    )
    assert upsert_titles(db_session, [meta]) == 1
    row = db_session.scalar(select(Title).where(Title.tmdb_id == 999002))
    assert row is not None
    assert row.vote_average == 7.4
    assert row.vote_count == 1_200


class _FakeCatalogSource:
    """Returns canned popular lists per kind (the ``CatalogSource`` protocol)."""

    def __init__(self) -> None:
        self.calls: list[tuple[TitleKind, int]] = []

    def popular(self, kind: TitleKind, page: int = 1) -> list[TitleMetadata]:
        self.calls.append((kind, page))
        base = 1000 if kind is TitleKind.movie else 2000
        return [
            TitleMetadata(
                kind=kind, tmdb_id=base + page, title=f"{kind.value}-{page}", popularity=float(page)
            )
        ]


def test_import_from_tmdb_pulls_each_kind_and_page(db_session: Session) -> None:
    source = _FakeCatalogSource()
    created = import_from_tmdb(db_session, source, pages=2)

    assert created == 4  # 2 kinds x 2 pages
    assert (TitleKind.movie, 1) in source.calls
    assert (TitleKind.show, 2) in source.calls


class _FakeDiscoverSource:
    """Genres + paged discover results (the ``CatalogDiscoverSource`` protocol)."""

    _GENRES = {
        TitleKind.movie: {28: "Action", 18: "Drama"},
        TitleKind.show: {18: "Drama"},
    }

    def __init__(self) -> None:
        self.calls: list[tuple[TitleKind, int | None, int]] = []

    def genres(self, kind: TitleKind) -> dict[int, str]:
        return dict(self._GENRES[kind])

    def discover(
        self,
        kind: TitleKind,
        *,
        genre_id: int | None = None,
        min_vote_count: int = 0,
        page: int = 1,
    ) -> list[TitleMetadata]:
        self.calls.append((kind, genre_id, page))
        if page > 1:
            return []  # one page of results per genre, then dry
        if kind is TitleKind.movie:
            return [
                # Shared across every movie genre -> must dedupe to one row.
                TitleMetadata(kind=kind, tmdb_id=7000, title="Shared", overview="o"),
                TitleMetadata(kind=kind, tmdb_id=8000 + (genre_id or 0), title="M", overview="p"),
                # No overview -> dropped (empty embedding input).
                TitleMetadata(kind=kind, tmdb_id=9000 + (genre_id or 0), title="Empty"),
            ]
        return [TitleMetadata(kind=kind, tmdb_id=6000, title="Show", overview="s")]


def test_broad_import_dedupes_filters_and_stops_when_dry(db_session: Session) -> None:
    source = _FakeDiscoverSource()
    created = broad_import_from_tmdb(db_session, source)

    # movie: shared(7000) + M8028 + M8018 ; show: 6000 -> the no-overview rows are dropped.
    assert created == 4
    assert db_session.scalar(select(Title).where(Title.tmdb_id == 7000)) is not None
    assert db_session.scalar(select(Title).where(Title.tmdb_id == 9028)) is None
    # Walked to the first empty page per genre, not all 20.
    assert (TitleKind.movie, 28, 2) in source.calls
    assert (TitleKind.movie, 28, 3) not in source.calls


class _FakeSearchSource:
    """Returns canned search matches (the ``CatalogSearchSource`` protocol)."""

    def search(self, query: str, *, limit: int = 8) -> list[TitleMetadata]:
        return [
            TitleMetadata(
                kind=TitleKind.movie,
                tmdb_id=555001,
                title="Searched Movie",
                year=2020,
                popularity=5.0,
                poster_path="/s.jpg",
            )
        ]


def test_search_titles_finds_local_match(db_session: Session) -> None:
    seed_sample_catalog(db_session)
    target = db_session.scalars(select(Title)).first()
    assert target is not None
    results = search_titles(db_session, target.title)
    assert target.id in {t.id for t in results}


def test_search_titles_upserts_live_matches(db_session: Session) -> None:
    results = search_titles(db_session, "searched", _FakeSearchSource())
    assert any(t.tmdb_id == 555001 for t in results)
    # The live match was persisted so it becomes recommendable + requestable.
    assert db_session.scalar(select(Title).where(Title.tmdb_id == 555001)) is not None


def test_search_titles_empty_query_returns_nothing(db_session: Session) -> None:
    seed_sample_catalog(db_session)
    assert search_titles(db_session, "   ") == []


def test_search_titles_ranks_prefix_above_more_popular_substring(db_session: Session) -> None:
    # "tenet" should lead with *Tenet*, not with a more popular title that merely contains the
    # letters mid-word — word-start matches rank above mid-word substrings, ahead of popularity.
    prefix = Title(kind=TitleKind.movie, tmdb_id=770001, title="Tenet", popularity=1.0)
    midword = Title(kind=TitleKind.movie, tmdb_id=770002, title="Subtenet", popularity=99.0)
    db_session.add_all([prefix, midword])
    db_session.flush()

    results = search_titles(db_session, "tenet")
    ids = [t.id for t in results]
    assert ids.index(prefix.id) < ids.index(midword.id)


def test_search_titles_ranks_word_start_above_midword(db_session: Session) -> None:
    # A match at the start of an *inner* word ("... Story") beats a mid-word substring, even when
    # the substring match is far more popular.
    word_start = Title(kind=TitleKind.movie, tmdb_id=770011, title="A Toy Story", popularity=1.0)
    midword = Title(kind=TitleKind.movie, tmdb_id=770012, title="Prehistory", popularity=99.0)
    db_session.add_all([word_start, midword])
    db_session.flush()

    results = search_titles(db_session, "story")
    ids = [t.id for t in results]
    assert ids.index(word_start.id) < ids.index(midword.id)


def test_search_vote_count_breaks_ties_within_a_lexical_tier(db_session: Session) -> None:
    # Round-8: among equally-lexical matches (both word-start on "inception"), the better-known one
    # (higher vote_count) leads and the junk tail sinks — kills "Bikini Inception" ranking next to
    # the real film. NULLS LAST so an unknown-vote junk title never floats above a voted one.
    good = Title(kind=TitleKind.movie, tmdb_id=880001, title="Inception", vote_count=34000)
    junk = Title(kind=TitleKind.movie, tmdb_id=880002, title="Inception Bikini", vote_count=3)
    unknown = Title(kind=TitleKind.movie, tmdb_id=880003, title="Inception Redux", vote_count=None)
    db_session.add_all([junk, unknown, good])
    db_session.flush()

    ids = [t.id for t in search_titles(db_session, "inception")]
    assert ids.index(good.id) < ids.index(junk.id)  # well-known leads its lexical tier
    assert ids.index(junk.id) < ids.index(unknown.id)  # a voted junk title still beats NULL votes


def test_search_obscure_exact_match_beats_high_vote_word_start(db_session: Session) -> None:
    # Vote count must NOT beat lexical relevance: an obscure *exact* title still leads over a much
    # better-known title that merely starts with the query.
    exact = Title(kind=TitleKind.movie, tmdb_id=881001, title="Her", vote_count=5)
    prefix = Title(kind=TitleKind.movie, tmdb_id=881002, title="Hercules", vote_count=90000)
    db_session.add_all([prefix, exact])
    db_session.flush()

    ids = [t.id for t in search_titles(db_session, "her")]
    assert ids.index(exact.id) < ids.index(prefix.id)  # exact tier wins despite far fewer votes


class _JunkFirstSearchSource:
    """A live source whose own ordering is junk-first — the merged ranking must override it."""

    def search(self, query: str, *, limit: int = 8) -> list[TitleMetadata]:
        return [
            TitleMetadata(
                kind=TitleKind.movie,
                tmdb_id=882002,
                title="Inception Bikini",
                vote_count=5,
            ),
            TitleMetadata(
                kind=TitleKind.movie,
                tmdb_id=882001,
                title="Inception",
                vote_count=34000,
            ),
        ]


def test_search_reranks_live_matches_instead_of_trusting_their_order(db_session: Session) -> None:
    # Live R8 repro: with a TMDB key configured, live matches used to be *prepended in TMDB's own
    # order*, shadowing the lexical-tier + vote-count ranking on every production search. The live
    # source is a discovery source only — the merged result must follow our ranking.
    results = search_titles(db_session, "inception", _JunkFirstSearchSource())
    tmdb_ids = [t.tmdb_id for t in results]
    assert tmdb_ids.index(882001) < tmdb_ids.index(882002)  # votes rank it, not TMDB order


def test_search_demotes_sub_floor_junk_below_above_floor_of_both_tiers(db_session: Session) -> None:
    # Sub-floor (< 50 votes) word-start junk must sink below *every* above-floor match — even a
    # mid-word substring match from the lower lexical tier — but still be findable, not dropped.
    word_junk = Title(kind=TitleKind.movie, tmdb_id=883001, title="Nova Junk", vote_count=3)
    word_good = Title(kind=TitleKind.movie, tmdb_id=883002, title="Nova Prime", vote_count=500)
    sub_good = Title(kind=TitleKind.movie, tmdb_id=883003, title="Supernova", vote_count=5000)
    db_session.add_all([word_junk, word_good, sub_good])
    db_session.flush()

    ids = [t.id for t in search_titles(db_session, "nova")]
    # Tier order among above-floor matches is untouched: word-start before mid-word substring.
    assert ids.index(word_good.id) < ids.index(sub_good.id)
    # The junk word-start match ranks after the above-floor match of the *lower* tier too.
    assert ids.index(sub_good.id) < ids.index(word_junk.id)
    assert word_junk.id in ids  # demoted, never dropped


def test_search_exact_tier_is_exempt_from_the_vote_floor(db_session: Session) -> None:
    # A user typing an exact obscure title must still find it first: the floor never touches the
    # exact tier, while a sub-floor *word-start* match still demotes behind above-floor matches.
    exact = Title(kind=TitleKind.movie, tmdb_id=884001, title="Her", vote_count=5)
    substring = Title(kind=TitleKind.movie, tmdb_id=884002, title="Teacher", vote_count=90000)
    word_junk = Title(kind=TitleKind.movie, tmdb_id=884003, title="Her Shadow", vote_count=3)
    db_session.add_all([substring, word_junk, exact])
    db_session.flush()

    ids = [t.id for t in search_titles(db_session, "her")]
    assert ids.index(exact.id) < ids.index(substring.id)  # 5-vote exact match still leads
    assert ids.index(substring.id) < ids.index(word_junk.id)  # sub-floor word-start demoted


class _CountingQueryEmbedder:
    """Embeds every text to one fixed direction; counts calls (the semantic tier allows ONE)."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [list(self.vector) for _ in texts]


def _unit_vector(*hot: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    for i in hot:
        vector[i] = 1.0
    return vector


def _embedded_title(
    session: Session,
    *,
    tmdb_id: int,
    title: str,
    vote_count: int | None,
    vector: list[float],
    version: str = "fake-embed",
    genres: list[str] | None = None,
) -> Title:
    # Genres default non-empty: the semantic tier excludes genre-less rows (skeletal documents the
    # heal hasn't enriched yet) — a test opts into that state with ``genres=[]``.
    row = Title(
        kind=TitleKind.movie,
        tmdb_id=tmdb_id,
        title=title,
        vote_count=vote_count,
        genres=["Drama"] if genres is None else genres,
    )
    session.add(row)
    session.flush()
    session.add(TitleEmbedding(title_id=row.id, model_version=version, embedding=vector))
    session.flush()
    return row


def test_search_semantic_fill_surfaces_nearest_titles_between_lead_and_junk(
    db_session: Session,
) -> None:
    # The live "ghibli" repro: lexical matches are one above-floor documentary and one 2-vote
    # bootleg — the semantic tier must fill the weak slots with the embedding-nearest catalog
    # titles, placed after the good lexical matches but before the demoted junk, floor applied,
    # lexical results excluded (no duplicates), all from ONE embedding call.
    bootleg = Title(kind=TitleKind.movie, tmdb_id=885001, title="Ghibli Concert", vote_count=2)
    db_session.add(bootleg)
    doc = _embedded_title(
        db_session, tmdb_id=885002, title="The Ghibli Story", vote_count=300, vector=_unit_vector(0)
    )
    spirited = _embedded_title(
        db_session, tmdb_id=885003, title="Spirited Away", vote_count=15000, vector=_unit_vector(0)
    )
    totoro = _embedded_title(
        db_session,
        tmdb_id=885004,
        title="My Neighbor Totoro",
        vote_count=8000,
        vector=_unit_vector(0, 1),  # a bit farther from the query than Spirited Away
    )
    far = _embedded_title(
        db_session, tmdb_id=885005, title="Unrelated Hit", vote_count=20000, vector=_unit_vector(1)
    )
    sub_floor_neighbor = _embedded_title(
        db_session, tmdb_id=885006, title="Obscure Gem", vote_count=3, vector=_unit_vector(0)
    )
    embedder = _CountingQueryEmbedder(_unit_vector(0))

    results = search_titles(db_session, "ghibli", embedder=embedder, embedding_version="fake-embed")
    ids = [t.id for t in results]
    # Lead lexical match first; semantic fills next in distance order; demoted junk last.
    assert ids[:4] == [doc.id, spirited.id, totoro.id, far.id]
    assert ids[-1] == bootleg.id
    assert doc.id not in ids[1:]  # already a lexical result — never duplicated by the fill
    assert sub_floor_neighbor.id not in ids  # an ANN neighbour with 3 votes is junk too
    assert embedder.calls == 1  # exactly one embedding call per search request


def test_search_semantic_fill_excludes_genreless_skeletal_documents(db_session: Session) -> None:
    # Live round-11 finding: a title upserted by discovery with no genres was embedded from a
    # skeletal document and ANN-matched wildly unrelated queries ("inception" AND "ghibli"). Until
    # the heal enriches + re-embeds it, it must not be offered as a semantic neighbour.
    lead = Title(kind=TitleKind.movie, tmdb_id=889001, title="Nova Prime", vote_count=500)
    db_session.add(lead)
    skeletal = _embedded_title(
        db_session,
        tmdb_id=889002,
        title="Eternal Soap",
        vote_count=700,  # well above the floor — the exclusion is about the document, not votes
        vector=_unit_vector(0),
        genres=[],
    )
    healed = _embedded_title(
        db_session, tmdb_id=889003, title="Real Neighbor", vote_count=700, vector=_unit_vector(0)
    )
    embedder = _CountingQueryEmbedder(_unit_vector(0))

    results = search_titles(db_session, "nova", embedder=embedder, embedding_version="fake-embed")
    ids = [t.id for t in results]
    assert healed.id in ids
    assert skeletal.id not in ids
    assert ids[0] == lead.id


def test_search_semantic_fill_skips_when_lexical_results_are_strong(db_session: Session) -> None:
    # 12 above-floor lexical matches = zero weak slots → the embedder must not even be called.
    db_session.add_all(
        Title(kind=TitleKind.movie, tmdb_id=886000 + i, title=f"Nova {i}", vote_count=100 + i)
        for i in range(12)
    )
    db_session.flush()
    embedder = _CountingQueryEmbedder(_unit_vector(0))

    results = search_titles(db_session, "nova", embedder=embedder, embedding_version="fake-embed")
    assert len(results) == 12
    assert embedder.calls == 0


def test_search_semantic_fill_skips_the_local_hash_space(db_session: Session) -> None:
    # Offline, retrieval runs on the meaningless local hash space: a hashed query vector would pull
    # in pure noise, so search stays exactly lexical — no embed call, same results as no embedder.
    junk = Title(kind=TitleKind.movie, tmdb_id=887001, title="Nova Junk", vote_count=3)
    db_session.add(junk)
    db_session.flush()
    embedder = _CountingQueryEmbedder(_unit_vector(0))

    with_embedder = search_titles(
        db_session, "nova", embedder=embedder, embedding_version=LOCAL_MODEL_VERSION
    )
    assert embedder.calls == 0
    assert [t.id for t in with_embedder] == [t.id for t in search_titles(db_session, "nova")]


class _ExplodingEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embed api down")


def test_search_survives_a_semantic_embed_failure(db_session: Session) -> None:
    # A query-embed hiccup degrades to lexical-only results, never a 500.
    junk = Title(kind=TitleKind.movie, tmdb_id=888001, title="Nova Junk", vote_count=3)
    db_session.add(junk)
    db_session.flush()

    results = search_titles(
        db_session, "nova", embedder=_ExplodingEmbedder(), embedding_version="fake-embed"
    )
    assert [t.id for t in results] == [junk.id]


def test_import_guard_blocks_dev_without_confirm() -> None:
    with pytest.raises(HTTPException) as exc:
        ensure_import_allowed(Settings(environment="development"), confirm=False)
    assert exc.value.status_code == 403


def test_import_guard_allows_with_confirm_or_production() -> None:
    # No raise in either case.
    ensure_import_allowed(Settings(environment="development"), confirm=True)
    ensure_import_allowed(Settings(environment="production"), confirm=False)


def test_cli_import_refuses_dev_without_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("TMDB_API_KEY", "x")
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli_app, ["import-catalog", "--scope", "broad"])
        assert result.exit_code == 2
        assert "Refusing to import" in result.output
    finally:
        get_settings.cache_clear()


def test_search_endpoint_returns_recommendation_items(db_session: Session) -> None:
    seed_sample_catalog(db_session)
    user = make_account(db_session)
    target = db_session.scalars(select(Title)).first()
    assert target is not None

    body = (
        authed_client(db_session, user)
        .post(f"/profiles/{user.profile.id}/catalog/search", json={"q": target.title})
        .json()
    )
    ids = {item["titleId"] for item in body["results"]}
    assert str(target.id) in ids
    assert "posterUrl" in body["results"][0]  # reuses the RecommendationItem DTO


def test_search_endpoint_serializes_ratings(db_session: Session) -> None:
    # Search cards show a compact "★ 8.4 · 37k" rating — the DTO must carry the Title row's vote
    # fields (nullable: an unrated row serializes null and the UI hides the line).
    user = make_account(db_session)
    rated = Title(
        kind=TitleKind.movie, tmdb_id=889001, title="Inception", vote_count=37000, vote_average=8.4
    )
    unrated = Title(kind=TitleKind.movie, tmdb_id=889002, title="Inception Redux")
    db_session.add_all([rated, unrated])
    db_session.flush()

    body = (
        authed_client(db_session, user)
        .post(f"/profiles/{user.profile.id}/catalog/search", json={"q": "inception"})
        .json()
    )
    by_id = {item["titleId"]: item for item in body["results"]}
    assert by_id[str(rated.id)]["voteAverage"] == 8.4
    assert by_id[str(rated.id)]["voteCount"] == 37000
    assert by_id[str(unrated.id)]["voteAverage"] is None
    assert by_id[str(unrated.id)]["voteCount"] is None
