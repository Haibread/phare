"""Per-title metadata heal — the shared primitive behind the read-path runtime enrichment and the
boot-time bulk runtime backfill.

The broad TMDB *discover* import omits runtime (and, historically, an insert bug dropped
``vote_average``), so most catalog rows land with ``runtime_minutes`` / ``vote_average`` /
``vote_count`` NULL. Both healing paths do the same two things — fetch a title's TMDB detail and
copy the missing fields onto the row without ever clobbering a value that's already there — so that
logic lives here once (rule of three: read-path :meth:`RecommendationService._enrich_runtimes`, the
bulk boot backfill, and any future caller share it).

Idempotent by construction: every write is guarded by ``is None``, so re-running only ever fills
holes. Best-effort per title: a flaky fetch is logged and skipped, never sinks the batch.

One deliberate exception to fill-only lives here too: :func:`apply_canonical_overwrite`, the
re-canonicalization primitive that *overwrites* localized text a past language-bound upsert wrote
into the canonical row (see docs/data-model.md, "Canonical vs localized text").
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import and_, delete
from sqlalchemy.orm import Session

from phare.db.models import Title, TitleEmbedding
from phare.providers.types import MetadataProvider, TitleMetadata

logger = logging.getLogger(__name__)

# Bounded concurrency for the per-title TMDB detail fan-out. httpx is thread-safe and the worker
# threads do *only* HTTP (never touch the session), so this parallelises the network wait without
# racing the DB. Shared by both heal paths so they can't drift apart.
RUNTIME_FETCH_WORKERS = 8


class RateLimiter:
    """A dead-simple thread-safe token bucket — one ``acquire()`` blocks until a token is free.

    Bounds the *bulk* metadata walker's request rate against TMDB (their guidance is ~50 rps; the
    8-worker fan-out on a warm CDN measured ~176 rps live, enough to trip a 429 storm that the
    keyset cursor would then skip whole batches over until the next boot). Wall-clock doesn't matter
    for a background job, so a plain steady drip is fine. The read path (chat ``_enrich_runtimes``,
    ≤64 fetches) is deliberately *not* throttled — it passes no limiter.

    ``clock``/``sleep`` are injectable so tests drive it on a fake clock with no real waiting.
    """

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        """Block until the next token is due, then reserve the following slot (steady drip)."""
        if self._interval <= 0.0:
            return
        with self._lock:
            now = self._clock()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._interval
        if wait > 0.0:
            self._sleep(wait)


def apply_metadata_heal(row: Title, meta: TitleMetadata) -> bool:
    """Copy missing metadata (runtime, votes, genres, credits, original language) from ``meta``.

    **Contract: ``meta`` must be canonical (language-neutral).** This is the shared write primitive
    behind every heal path, and some of what it fills is *text* the whole engine treats as
    English-canonical — ``genres`` feed the SQL genre filters and the embedding document, credits
    feed the document too. A localized fetch (TMDB with ``language=fr``) carries French genre
    labels; persisting those would break every genre filter and cluster the embedding space by
    language (see docs/data-model.md, "Canonical vs localized text"). Callers holding a
    request-language provider must fetch through its ``canonical()`` view
    (:func:`phare.providers.types.canonical_source`) before handing metadata here.

    Never clobbers an existing value (idempotent — only fills holes: a NULL scalar or an *empty*
    array). Returns True when *anything* was filled — the signal the gap gauge and the caller
    use to count progress; a title whose runtime was already present but whose credits/language were
    healed still counts. The caller owns the commit.

    Filling a field that feeds the embedding document (genres, credits, language — see
    :func:`phare.embeddings.service.build_embedding_text`) also drops the title's stale embeddings:
    a vector computed from a skeletal document ("title + overview" only) lands in a meaningless
    neighbourhood — measured live, one genre-less show surfaced as a semantic search neighbour for
    both "inception" and "ghibli". The read-path embedding backfill re-embeds the enriched document
    on the next recommendation pass, so the heal stays self-contained and self-correcting.
    """
    filled = False
    doc_enriched = False
    if row.runtime_minutes is None and meta.runtime_minutes is not None:
        row.runtime_minutes = meta.runtime_minutes
        filled = True
    if row.vote_average is None and meta.vote_average is not None:
        row.vote_average = meta.vote_average
    if row.vote_count is None and meta.vote_count is not None:
        row.vote_count = meta.vote_count
    # Arrays are empty-by-default, so "missing" is empty, not NULL. Fill only when we have
    # something to write, so an unfillable fetch doesn't flip the progress signal for nothing.
    if not row.genres and meta.genres:
        row.genres = list(meta.genres)
        filled = True
        doc_enriched = True
    if not row.directors and meta.directors:
        row.directors = list(meta.directors)
        filled = True
        doc_enriched = True
    if not row.top_cast and meta.top_cast:
        row.top_cast = list(meta.top_cast)
        filled = True
        doc_enriched = True
    if row.original_language is None and meta.original_language is not None:
        row.original_language = meta.original_language
        filled = True
        doc_enriched = True
    if doc_enriched:
        _drop_stale_embeddings(row)
    return filled


def has_metadata_gap(row: Title) -> bool:
    """Python twin of the bulk walker's SQL gap predicate
    (:func:`phare.catalog.runtime_backfill._metadata_gap_predicate`): the row is missing a runtime,
    an original language, or genres. Language is the honest "never deep-fetched" sentinel — one
    detail fetch carries runtime, votes, genres, credits *and* language, so a row with a language
    already got whatever else TMDB had. Used by the read-path heals to decide whether a detail open
    is worth a canonical fetch at all."""
    return (
        row.runtime_minutes is None or row.original_language is None or len(row.genres or []) == 0
    )


# Compact French-function-word detector for the re-canonicalization pass: an overview containing
# two or more of these words (word-bounded, case-insensitive) reads as French. Tuned against the
# measured production contamination (66 rows upserted from language-bound search); two-plus hits
# keeps English overviews with an incidental "LA"/"pour" from matching. Kept as one alternation so
# the SQL predicate and its Python twin can't drift.
_FRENCH_FUNCTION_WORDS = "le|la|les|une|des|dans|avec|pour|qui"
# PG flavour: \m/\M are POSIX word boundaries; matched with the case-insensitive ~* operator.
LOCALIZED_OVERVIEW_SQL_PATTERN = rf"\m({_FRENCH_FUNCTION_WORDS})\M.*\m({_FRENCH_FUNCTION_WORDS})\M"
_LOCALIZED_OVERVIEW_RE = re.compile(
    rf"\b({_FRENCH_FUNCTION_WORDS})\b.*\b({_FRENCH_FUNCTION_WORDS})\b",
    re.IGNORECASE | re.DOTALL,
)


def localized_text_predicate() -> Any:
    """SQL predicate: the row's stored overview reads as French *and* the title isn't French-origin.

    Detects canonical ``Title`` rows contaminated with localized text (upserted from a
    language-bound live search before the canonical-writes rule). The ``original_language`` guard
    is the convergence guard: a French film's canonical overview may legitimately be French (TMDB
    has no English translation), so those rows are exempt rather than re-fetched forever. A NULL
    language doesn't exempt — the same walker pass fills it, and the Python twin re-checks after."""
    return and_(
        Title.overview.op("~*")(LOCALIZED_OVERVIEW_SQL_PATTERN),
        Title.original_language.is_distinct_from("fr"),
    )


def looks_localized(row: Title) -> bool:
    """Python twin of :func:`localized_text_predicate`, evaluated after a heal may have just filled
    ``original_language`` — so a row the fetch reveals to be French-origin is exempted before any
    overwrite."""
    return (
        row.original_language != "fr"
        and bool(row.overview)
        and _LOCALIZED_OVERVIEW_RE.search(row.overview or "") is not None
    )


def apply_canonical_overwrite(row: Title, meta: TitleMetadata) -> bool:
    """Overwrite a row's display/document text (``title``, ``overview``) with canonical metadata.

    The **one deliberate exception** to :func:`apply_metadata_heal`'s fill-only rule: a row flagged
    by :func:`looks_localized` holds *wrong-language* text (a localized search upsert wrote French
    into the canonical row — e.g. "Kara Sevda" stored as "Amour éternel"), and filling holes can't
    fix text that's present-but-poisoned. Only the re-canonicalization pass calls this, and only
    for flagged rows; ``meta`` must be a canonical (language-neutral) fetch.

    Empty canonical fields never overwrite (TMDB returns an empty overview when a title has no
    English translation — blanking the row would starve its embedding document; the row stays
    flagged and costs one no-op fetch per boot pass, same trade-off as the poisoned-embeddings
    trigger). When anything changed the row's embeddings are dropped — the old vector encodes the
    French document, which is exactly the language-clustering the pass exists to kill — and the
    lazy read-path backfill re-embeds the canonical document. Returns True when anything changed,
    so an identical canonical text converges to a no-op instead of rewriting forever.
    """
    changed = False
    if meta.title and meta.title != row.title:
        row.title = meta.title
        changed = True
    if meta.overview and meta.overview != row.overview:
        row.overview = meta.overview
        changed = True
    if changed:
        _drop_stale_embeddings(row)
        logger.info("catalog.heal.recanonicalized", extra={"title_id": str(row.id)})
    return changed


def _drop_stale_embeddings(row: Title) -> None:
    """Delete ``row``'s embeddings (all spaces) after its document-relevant metadata was enriched.

    All spaces, not just the write target: the old vector encodes the skeletal document wherever it
    lives, and a transitional (pre-cutover) space is on its way out anyway. Between the drop and the
    lazy re-embed the title simply doesn't ANN-match — strictly better than matching everything.
    """
    session = Session.object_session(row)
    if session is None:  # detached test fixture — nothing persisted to invalidate
        return
    session.execute(delete(TitleEmbedding).where(TitleEmbedding.title_id == row.id))
    logger.info("catalog.heal.embeddings_dropped", extra={"title_id": str(row.id)})


def fetch_metadata_parallel(
    source: MetadataProvider,
    rows: Iterable[Title],
    *,
    limiter: RateLimiter | None = None,
) -> dict[uuid.UUID, TitleMetadata]:
    """Fetch TMDB detail for each row with a ``tmdb_id``, in parallel (bounded concurrency).

    Returns ``{title_id: metadata}`` for the fetches that succeeded — a failed or empty fetch is
    logged and simply absent. The worker threads only do HTTP; the caller applies the results to
    session-bound rows on the main thread (see :func:`apply_metadata_heal`).

    ``limiter`` (optional) caps the request rate before each fetch — passed by the bulk walker to
    stay well under TMDB's rps guidance; the read path omits it so chat turns aren't slowed. A 429
    still triggers the provider's own ``Retry-After`` retry inside ``get_title``.
    """
    fetchable = [(row.id, row.tmdb_id, row.kind) for row in rows if row.tmdb_id is not None]
    if not fetchable:
        return {}

    def fetch(item: tuple[uuid.UUID, int, object]) -> tuple[uuid.UUID, TitleMetadata | None]:
        title_id, tmdb_id, kind = item
        if limiter is not None:
            limiter.acquire()
        try:
            meta = source.get_title(tmdb_id, kind)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - a flaky fetch must not sink the batch
            logger.warning("catalog.heal.fetch_failed", extra={"title_id": str(title_id)})
            return title_id, None
        return title_id, meta

    out: dict[uuid.UUID, TitleMetadata] = {}
    with ThreadPoolExecutor(max_workers=RUNTIME_FETCH_WORKERS) as pool:
        for title_id, meta in pool.map(fetch, fetchable):
            if meta is not None:
                out[title_id] = meta
    return out
