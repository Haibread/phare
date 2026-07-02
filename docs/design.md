# Design

## Engine: LLM steers, embeddings rank

Pipeline: `events → taste extraction (LLM) → taste profile → candidate generation (vector +
filters) → re-ranker → explanations (LLM)`.

- **Taste extraction** (LLM, batch/on-demand, cheap): reads the profile's events — including the
  signals others throw away: **abandonment, low ratings, rewatches, free-text feedback** — into
  a structured taste profile.
- **Candidate generation:** vector search around the taste vector + hard filters (exclude
  watched, apply hard-avoids, apply chat intent filters). The pgvector HNSW index is *approximate*,
  so the search widens `hnsw.ef_search` and breaks distance ties on the stable catalog id — the
  same profile gets the same candidates every load, keeping the re-ranker below deterministic.
- **Re-ranker (deterministic — where steering happens):** similarity × profile affinity ×
  **recency-decayed** taste, then **anti-degeneracy**: cap popularity, apply a **quality floor**
  (penalise titles rated below ~6/10 on TMDB, never boost above it), enforce diversity,
  guarantee catalog coverage over time.
- **Swing-for-the-fences:** every slate reserves a few deliberate high-novelty picks, *not*
  judged on accuracy. Discovery is the point; pure accuracy yields a popularity machine.
- **Explanations (LLM):** short, spoiler-safe, never cite another user, express confidence.
  **Grounded** in the title's synopsis + keywords so the model describes real appeal instead of
  inventing aligned qualities, and told the actual fit strength so a weak pick is framed as a
  stretch, not oversold (review H4). The synopsis is for grounding only — the prompt forbids
  retelling plot and the output is spoiler-screened; the offline template never touches it.

Why not LLM-as-ranker: it can't recall a large catalog, hallucinates titles, and is slower and
costlier. It's good at reading fuzzy human signal — so that's all it does.

## Two layers

- **Stable taste** (who you are) → **UI rows**, precomputed/cached.
- **Ephemeral mood/intent** ("tired, 90 min, funny") → **chat agent**, applied as extra
  filters/boosts over the same engine.

## Rows

- `watch_again` — from own history.
- `you_might_like` — the full pipeline. **This is the actual product**; most value/risk is here.
- `popular` — global popularity.
- `continue_watching` / `next_up` — in-progress shows, next episodes (TV roll-up).

Each item carries a per-item **confidence** [0,1] that the UI renders as a coarse, worded "fit"
chip (never a number — see *honesty over engagement*). Only `you_might_like` derives it from the
taste-vector match — and from the pick's **pool-relative** similarity (where it sits among that
query's candidates), not the raw cosine, which is compressed near the top and would read "strong
fit" for everything (review H2/A8). A lightly-evidenced taste profile also *caps* the chip, so a
thin history can't produce a blanket "strong fit"; and a title with almost no votes (a just-dropped
release, no quality signal yet) is capped too, so it can surface as a discovery pick but never as a
sure thing (review A9) — it graduates on its own as it accrues votes. The heuristic rows expose an
honest signal of
their *own* kind, not a fake taste score:

- `watch_again` → your own rating (rating ÷ 10), so the row reads "strong" because it's literally
  your top-rated titles, sorted best-first. A show you're partway through is **excluded** here —
  it belongs in `continue_watching`, and the same series in both rows is a visible contradiction
  (review A11).
- `continue_watching` → recency decay on the last episode you watched (half-life ~6 weeks): a fresh
  thread reads strong, an abandoned one cools off.

Items the profile has already watched carry a `watched` flag, so the UI badges them "Watched" — most
visibly in **search**, where a title you've seen can turn up and should say so (review A11).
- `popular` → popularity magnitude on a log scale (a runaway hit reads stronger than a mild one).

Because those last two aren't a *taste-fit* signal, the UI **doesn't render the fit meter** on the
`continue_watching` and `popular` rows — the same worded gauge meaning "how far in" or "how
well-known" on some rows and "how well it fits you" on others would make one label mean several
things (review H8). Only the taste-driven rows (`you_might_like`, `because…`, dynamic themes) show it.

**Page hygiene:** a title appears at most **twice** across the whole page, applied in row-priority
order so the strongest rows keep it (this is what stops "The Wire" turning up in the hero,
you_might_like, popular *and* a theme — review A7); the frontend also drops the hero pick from the
rows below so it isn't shown twice. A `because you watched X` row left with fewer than a few items
(a lone odd neighbour) is hidden rather than shown looking broken (A10).

A row with genuinely no opinion would pass `confidence = null`; the UI then shows the lowest
neutral chip. (Today every row computes a real value, so that path is just the graceful fallback.)
- **Dynamic LLM-generated rows** — an agent picks the day's rows from profile + mood + calendar
  ("late October → horror you'd tolerate"; "finished Dune → the Villeneuve rabbit-hole"). Cheap
  differentiator.

## Scope of recommendations

Primary: **recommend from the whole universe** (great content, period). Optional, behind action
providers: watch on a linked platform if available, or **request** via Radarr/Sonarr/Jellyseerr.
A "from my library / subscriptions / region only" mode is just a candidate-set filter on the same
engine — supported, not the headline.

## Privacy & safety (hard rules)

- **Per-profile isolation**, enforced at the query layer: a profile reads only its own history,
  ratings, taste, recommendations. No UI/API path to another user's data.
- **No cross-user (collaborative) signal in v1.**
- **The LLM never cites another user** in any output ("because Bob liked this" is a leak).
- **Spoiler safety:** LLM output describes *appeal* (tone, themes, fit), never plot events of
  unwatched content.
- Open-source hygiene: never ship real data/logs; secrets only in the backend.

## Deferred — do NOT build these yet

Recording so agents don't pre-build them. Each needs a fresh decision before starting:
collaborative filtering (privacy-safe, aggregated; only when N grows) · household / co-watching ·
Plex/Jellyfin as *sources* · CSV import · MCP servers for providers · availability/region data
(e.g. JustWatch).
