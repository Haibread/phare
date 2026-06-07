# Data model

Only the semantics that aren't obvious from a schema file. Concrete tables/columns/migrations
are the implementer's job (follow the sql-conventions skill).

## Canonical identity

One canonical title per work, keyed by **TMDB (primary) + IMDb (secondary)**. Every source
resolves to it; source-specific ids never leak past ingestion. TMDB is also the source of
embedding inputs (overview, genres, keywords, cast/crew, tone) and the `popular` signal.

## TV is a tree

`show → season → episode`. **Recommend at show level**; collect signal at every level and roll
it up via one tested function:

- finished a season / bingeing → **strong positive** on the show;
- dropped after the pilot → **strong negative**;
- mid-season abandonment → negative, weighted by progress;
- per-episode ratings aggregate to a show signal (recent-weighted);
- a started show counts as "watched" (not re-recommended as new) but stays eligible for
  continue/next-up.

Embed at **title** level (show/movie). Episodes/seasons exist for tracking and signal, not
similarity search.

## Normalized event stream (the heart)

Every source normalizes into one per-profile event shape:
`type ∈ {watched, rated, liked, disliked, abandoned, rewatched, watchlisted}`, canonical
title (+ season/episode for TV), rating normalized to 0–10, optional **free-text** (the LLM
reads it), `occurred_at` (drives recency decay), `source` provenance, and an `excluded` flag.

**Dedup / conflict:** the same logical event from two sources (watched on Plex *and* synced to
Trakt) collapses to one; keep all provenances. On rating conflict, pick a deterministic winner
(default most-recent, configurable) — never silently average. Re-syncs must be idempotent.

## Taste profile

Per profile: a structured object (likes / dislikes / **hard-avoids** / weighted affinities /
comfort axis / discovery tolerance) + a human-readable summary + a **confidence** (drives "we're
guessing" honesty). User edits live separately and **always win**, surviving regeneration.
Recency decay applies wherever taste is computed.

## Recommendation log

Every recommendation shown (any surface) is logged with its context (title, surface, rank, taste
snapshot, whether it was a swing slot, timestamp) and **never deleted** — this powers the
closed-loop metric in [`evaluation.md`](evaluation.md). Must exist from the first rec rendered.

## One account = one user

No shared-account persona modeling. The only concession to a previously-shared Trakt/Plex
account is a one-time **import cleanup** at onboarding: bulk-exclude mixed history (sets
`excluded`); excluded events are ignored by taste modeling.

## Embeddings are versioned

Store a `model_version` with each vector. Swapping the embedding model triggers a full re-embed;
never mix versions in one vector space.
