# Data model

Only the semantics that aren't obvious from a schema file. Concrete tables/columns/migrations
are the implementer's job (follow the sql-conventions skill).

## Canonical identity

One canonical title per work, keyed by **TMDB (primary) + IMDb (secondary)**. Every source
resolves to it; source-specific ids never leak past ingestion. TMDB is also the source of
embedding inputs (overview, genres, keywords, cast/crew, tone) and the `popular` signal.

TMDB's movie and TV id spaces are **disjoint** — the same numeric id can name a film and an
unrelated show (1398 is both *Stalker* and *The Sopranos*) — so a title is unique on
**`(tmdb_id, kind)`**, never `tmdb_id` alone, and every title lookup carries the kind. (IMDb ids
*are* globally unique, so `imdb_id` stays singularly unique.) Getting this wrong silently merged a
movie and a show and mis-attached watch history — review H3a.

## Title metadata columns

Beyond identity, a title carries display + steering metadata, all filled from TMDB (import or the
lazy/background heal, never guessed): `overview`, `genres`, `keywords`, `runtime_minutes`,
`popularity`, `vote_count` (how *many* rated it — a known-ness proxy), `vote_average` (how *well* —
a crude quality floor). Round 8 adds:

- **`directors`** / **`top_cast`** — plain-name arrays (director(s) for a movie, creator(s) for a
  show; first ~5 billed cast). Empty by default; fetched via TMDB `append_to_response=credits`
  (movies) / `created_by` + `aggregate_credits` (TV) in the *same* detail request, so credits cost
  no extra HTTP call. Surfaced on the title-detail API (`directors`, `topCast`), not yet embedded.
- **`original_language`** — TMDB ISO-639-1 origin code (`en`, `ja`, …), nullable. Wired through the
  broad *discover* import (it carries the field) so new imports get it for free, and healed for
  older rows. Groundwork for true anime handling (Animation + `ja` origin); the current "anime"
  handling is only a genre alias → Animation (see `recommend/genres.py`).

The heal never clobbers a non-empty value — it only fills holes (a NULL scalar or an empty credit
array), so it's idempotent across re-runs.

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
`type ∈ {watched, rated, liked, disliked, abandoned, rewatched, watchlisted, not_interested}`,
canonical title (+ season/episode for TV), rating normalized to 0–10, optional **free-text** (the
LLM reads it), `occurred_at` (drives recency decay), `source` provenance, and an `excluded` flag.

`not_interested` is the one type a user emits **directly from the UI**: the "not interested" button
in a title's detail sheet (opened by tapping its card) — a negative taste contribution (weight −1.0)
that, being an event on the title, also drops it from future candidate generation. It sits in the
sheet, behind a deliberate tap, rather than on the card itself, so a stray tap on a card's primary
target can't emit a destructive signal. It's distinct from `disliked`, which implies
the title was actually watched. The write reuses the chat agent's undo mechanism: the endpoint
returns an `event:<id>` token the client hands to `/chat/undo` to reverse it. No positive card
signal exists — deliberately, to avoid optimizing for engagement.

**Dedup / conflict:** the same logical event from two sources (watched on Plex *and* synced to
Trakt) collapses to one; keep all provenances. On rating conflict, pick a deterministic winner
(default most-recent, configurable) — never silently average. Re-syncs must be idempotent.

### Derived signals (rewatch, abandonment)

Sources only ever emit `watched`, `rated`, and `watchlisted` — never `rewatched`, `abandoned`,
`liked`, or `disliked`. But "the signals others throw away" (see [`design.md`](design.md)) are
the point, so the engine **synthesizes** them deterministically when building the taste centroid
(`recommend/taste_vector.py`), by collapsing a title's `watched` events into one signal:

- **Rewatch** — a movie with ≥2 `watched` events becomes one `rewatched` (strongest comfort
  weight) instead of two stacked `watched` rows.
- **Abandonment** — a show with ≥2 distinct watched episodes whose last episode is **stale**
  (older than `_ABANDON_STALE_DAYS`, default 180) **and that was never rated** becomes one
  `abandoned` (negative weight). It's conservative on purpose: we have no total-episode count, so
  abandonment can't be proven, and a rating — high *or* low — is an explicit verdict we trust over
  the heuristic (otherwise a finished, loved show rated 10 looks identical to one you bailed on).

Low ratings already feed negative signal through the `rated` path, so they need no synthesis.
Ratings and watchlist entries stay per-event; only `watched` events collapse.

## Taste profile

Per profile: a structured object (likes / dislikes / **hard-avoids** / weighted affinities /
comfort axis / discovery tolerance) + a human-readable summary + a **confidence** (drives "we're
guessing" honesty). User edits live separately and **always win**, surviving regeneration.
Recency decay applies wherever taste is computed.

When the LLM extraction fails, the profile falls back to a genre-frequency read and is flagged
**`degraded`**; the auto-refresh then re-attempts extraction on the next trigger (in case the
provider recovered) instead of freezing the coarse profile, and clears the flag on success — user
overrides survive throughout (review A14).

`affinities` keys and `hard-avoids` are the fields that actually **steer scoring**, so the
extractor draws them from a **closed vocabulary** (canonical TMDB genres + a small controlled set
of tone/era descriptors) and they're matched against a candidate's genres/keywords by a shared,
tolerant rule (alias-resolved equality, or a ≥4-char substring). `likes` / `dislikes` / `summary`
stay free-form. This is what makes affinity actually weigh on the ranking instead of silently
reading neutral for everyone — a free key like `"Sci-Fi"` now lines up with the catalog's
`"Science Fiction"` tag (review H1). A key that resolves to nothing in the vocabulary is kept but
logged (`taste.affinity_key_unmatched`) so a dead profile is visible, not silent.

## Agent memory (commitments + notes)

The chat agent ([`agent.md`](agent.md)) writes back, so two structured, per-profile, inspectable
stores back its long-term memory (chat is just another event `source="chat"` in the stream above):

- **WatchCommitment** — an "I'll watch X" intent: `title`, `status` (`pending|watched|dropped`),
  `note`, timestamps. Resolving one (watched/dropped) is what turns a plan into a real signal; open
  ones drive the next-session follow-up.
- **MemoryNote** — free-text generalist memory: `text`, `kind` (`preference|context|fact`), optional
  `expires_at` (temporal context), `source`. A **steering input, never a ranking authority**:
  durable preferences distil into the taste profile's overrides; temporal notes only colour the
  active session and feed the taste-extraction prompt. Editable like the taste profile.

## Recommendation log

Every recommendation shown (any surface) is logged with its context (title, surface, rank, taste
snapshot, whether it was a swing slot, timestamp) and **never deleted** — this powers the
closed-loop metric in [`evaluation.md`](evaluation.md). Must exist from the first rec rendered.

## One account = one user

Literal: a `user` (credentials) owns exactly one `profile` (taste/history), 1:1. How a user
authenticates lives in `identity` rows — a local password, Plex, … — keyed on `(provider,
subject)` so new providers add rows, not columns. No shared-account persona modeling. The only
concession to a previously-shared Trakt/Plex account is a one-time **import cleanup** at
onboarding: bulk-exclude mixed history (sets `excluded`); excluded events are ignored by taste
modeling. The auth model (tokens, isolation, Sign in with Plex) is specified in [`auth.md`](auth.md).

## Embeddings are versioned

Store a `model_version` with each vector. Swapping the embedding model triggers a full re-embed;
never mix versions in one vector space.
