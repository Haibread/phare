"""Offline sample catalog — a diverse pool of unwatched titles to recommend *from*.

Lets the engine produce real rows (and E2E exercise the full pipeline) with zero TMDB/Trakt
credentials. Deliberately spans genres, eras, tones, and movie/show so re-ranker diversity and
swing slots have something to chew on. Seeded idempotently by ``tmdb_id`` (none of these clash
with the sample *history* in ``ingest/sample.py``: 438631 / 68718 / 95396).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from phare.catalog.service import upsert_titles
from phare.db.models import TitleKind
from phare.providers.types import TitleMetadata


def _movie(
    tmdb_id: int,
    title: str,
    year: int,
    genres: list[str],
    keywords: list[str],
    runtime: int,
    popularity: float,
    overview: str,
) -> TitleMetadata:
    return TitleMetadata(
        kind=TitleKind.movie,
        tmdb_id=tmdb_id,
        title=title,
        year=year,
        genres=genres,
        keywords=keywords,
        runtime_minutes=runtime,
        popularity=popularity,
        overview=overview,
    )


def _show(
    tmdb_id: int,
    title: str,
    year: int,
    genres: list[str],
    keywords: list[str],
    runtime: int,
    popularity: float,
    overview: str,
) -> TitleMetadata:
    return TitleMetadata(
        kind=TitleKind.show,
        tmdb_id=tmdb_id,
        title=title,
        year=year,
        genres=genres,
        keywords=keywords,
        runtime_minutes=runtime,
        popularity=popularity,
        overview=overview,
    )


_SAMPLE_CATALOG: list[TitleMetadata] = [
    _movie(
        329865,
        "Arrival",
        2016,
        ["Science Fiction", "Drama"],
        ["aliens", "linguistics", "time"],
        116,
        35.0,
        "A linguist races to communicate with extraterrestrial visitors.",
    ),
    _movie(
        335984,
        "Blade Runner 2049",
        2017,
        ["Science Fiction", "Mystery"],
        ["replicant", "dystopia", "neo-noir"],
        164,
        40.0,
        "A young blade runner uncovers a secret that could plunge society into chaos.",
    ),
    _movie(
        264660,
        "Ex Machina",
        2014,
        ["Science Fiction", "Thriller"],
        ["artificial intelligence", "android", "isolation"],
        108,
        22.0,
        "A programmer is invited to administer a Turing test on an intelligent humanoid robot.",
    ),
    _movie(
        157336,
        "Interstellar",
        2014,
        ["Science Fiction", "Adventure"],
        ["wormhole", "space", "time dilation"],
        169,
        80.0,
        "Explorers travel through a wormhole in search of a new home for humanity.",
    ),
    _movie(
        238,
        "The Godfather",
        1972,
        ["Drama", "Crime"],
        ["mafia", "family", "power"],
        175,
        70.0,
        "The aging patriarch of a crime dynasty transfers control to his reluctant son.",
    ),
    _movie(
        8967,
        "There Will Be Blood",
        2007,
        ["Drama"],
        ["oil", "greed", "ambition"],
        158,
        18.0,
        "A ruthless prospector builds an empire during Southern California's oil boom.",
    ),
    _movie(
        120467,
        "The Grand Budapest Hotel",
        2014,
        ["Comedy", "Drama"],
        ["whimsical", "heist", "concierge"],
        99,
        28.0,
        "A legendary concierge and his protege are framed for murder.",
    ),
    _movie(
        8363,
        "Superbad",
        2007,
        ["Comedy"],
        ["coming of age", "high school", "friendship"],
        113,
        20.0,
        "Two co-dependent high schoolers scheme to buy alcohol for a party.",
    ),
    _movie(
        493922,
        "Hereditary",
        2018,
        ["Horror", "Mystery"],
        ["grief", "occult", "family secrets"],
        127,
        30.0,
        "A grieving family is haunted by tragic and disturbing occurrences.",
    ),
    _movie(
        419430,
        "Get Out",
        2017,
        ["Horror", "Thriller"],
        ["social commentary", "hypnosis", "suburbia"],
        104,
        33.0,
        "A young man uncovers a disturbing secret on a visit to his girlfriend's family.",
    ),
    _movie(
        76341,
        "Mad Max: Fury Road",
        2015,
        ["Action", "Adventure", "Science Fiction"],
        ["post-apocalyptic", "chase", "desert"],
        120,
        55.0,
        "A drifter and a rebel flee a tyrant across a wasteland in a war rig.",
    ),
    _movie(
        245891,
        "John Wick",
        2014,
        ["Action", "Thriller"],
        ["assassin", "revenge", "gun-fu"],
        101,
        60.0,
        "A retired hitman seeks vengeance for the killing of his dog.",
    ),
    _movie(
        120,
        "The Lord of the Rings: The Fellowship of the Ring",
        2001,
        ["Fantasy", "Adventure"],
        ["quest", "middle earth", "ring"],
        178,
        65.0,
        "A hobbit sets out to destroy a powerful ring and the dark lord who forged it.",
    ),
    _movie(
        6977,
        "No Country for Old Men",
        2007,
        ["Western", "Crime", "Thriller"],
        ["chase", "money", "fate"],
        122,
        24.0,
        "A hunter stumbles on drug-deal money and is pursued by a relentless killer.",
    ),
    _movie(
        210577,
        "Gone Girl",
        2014,
        ["Mystery", "Thriller", "Drama"],
        ["marriage", "missing person", "media"],
        149,
        38.0,
        "A man becomes the prime suspect in the disappearance of his wife.",
    ),
    _movie(
        146233,
        "Prisoners",
        2013,
        ["Thriller", "Crime", "Drama"],
        ["abduction", "moral dilemma", "investigation"],
        153,
        26.0,
        "A father takes matters into his own hands when his daughter goes missing.",
    ),
    _movie(
        129,
        "Spirited Away",
        2001,
        ["Animation", "Fantasy", "Family"],
        ["spirits", "coming of age", "magic"],
        125,
        50.0,
        "A girl wanders into a world of spirits and must work to free her parents.",
    ),
    _movie(
        324857,
        "Spider-Man: Into the Spider-Verse",
        2018,
        ["Animation", "Action", "Adventure"],
        ["multiverse", "superhero", "coming of age"],
        117,
        58.0,
        "A teen becomes Spider-Man and meets heroes from parallel dimensions.",
    ),
    _show(
        1396,
        "Breaking Bad",
        2008,
        ["Drama", "Crime"],
        ["chemistry teacher", "drugs", "transformation"],
        47,
        90.0,
        "A chemistry teacher turns to manufacturing drugs after a cancer diagnosis.",
    ),
    _show(
        1438,
        "The Wire",
        2002,
        ["Drama", "Crime"],
        ["police", "baltimore", "institutions"],
        60,
        40.0,
        "The drug trade in Baltimore seen through the eyes of law enforcement and dealers.",
    ),
    _show(
        87108,
        "Chernobyl",
        2019,
        ["Drama", "History"],
        ["disaster", "nuclear", "cover-up"],
        60,
        45.0,
        "A dramatization of the 1986 nuclear disaster and the sacrifices to contain it.",
    ),
    _show(
        60059,
        "Better Call Saul",
        2015,
        ["Drama", "Crime"],
        ["lawyer", "prequel", "moral decline"],
        47,
        52.0,
        "The transformation of a small-time lawyer into a morally flexible attorney.",
    ),
    _show(
        94997,
        "House of the Dragon",
        2022,
        ["Drama", "Fantasy", "Action"],
        ["dragons", "succession", "civil war"],
        60,
        85.0,
        "The Targaryen dynasty fractures into a war of succession over the throne.",
    ),
    _show(
        46648,
        "True Detective",
        2014,
        ["Drama", "Crime", "Mystery"],
        ["detectives", "occult", "anthology"],
        55,
        36.0,
        "Detectives chase a ritualistic killer across decades in Louisiana.",
    ),
]


def sample_catalog_metas() -> list[TitleMetadata]:
    """The sample catalog as metadata (used by the evaluation personas)."""
    return list(_SAMPLE_CATALOG)


def seed_sample_catalog(session: Session) -> int:
    """Seed the offline sample catalog. Idempotent; returns the number of titles created."""
    return upsert_titles(session, _SAMPLE_CATALOG)
