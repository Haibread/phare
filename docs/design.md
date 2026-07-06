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
  - **Graded affinity (review R1).** The affinity term measures *how much of a profile's taste a
    candidate satisfies*, not just whether it hits anything. It used to sum the matched affinity
    weights and clamp to 1, so a real profile (a dozen-plus positives weighted 0.3–0.9) saturated:
    matching any two liked genres already read a flat 1.0, and the fit meter it feeds collapsed to
    "strong" for the whole slate. Now each match contributes its weight against a **saturating
    budget** — the sum of the profile's four strongest same-sign weights: `pos = min(1, Σ matched
    positive weights ÷ budget)`, `neg = min(1, Σ |matched negative weights| ÷ budget)`, and
    `affinity = pos − neg` in [−1, 1] (mapped to [0, 1] with 0.5 = neutral for scoring). The
    budget is capped at the strongest few keys rather than the whole profile because a title carries
    only a handful of genres/keywords and can realistically satisfy only a few affinities;
    normalising against every key would crush every genuine match toward neutral. Consequences: a
    candidate matching **three strong** positives scores visibly above one matching **a single
    mid-weight** key (measured spread ≈ 0.26 on a real profile); a candidate matching **nothing**
    stays at the neutral 0.5 — vocabulary silence is never punished (honesty, principle 4);
    **negative** affinities pull the score down proportionally; and hard-avoids remain a **separate
    upstream filter**, untouched by this term. Deterministic, cheap, no LLM.
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
- `popular` — global popularity. **Selected and ordered by popularity** (that's the row's identity),
  but its fit gauge reads an **honest taste fit**, not popularity magnitude (lot R6b): each popular
  title is scored against the profile's real taste — similarity of its embedding to the taste
  centroid, folded with the graded affinity through the same `_confidence` blend the taste rows use
  (unproven cap included, since popular skews recent) — so a blockbuster that doesn't fit you reads
  as a long shot, not a confident pick. It still **respects the profile's `hard_avoids`**: a title
  matching a hard-avoid is never served here, so "popular" means "popular *and* something you'd
  actually watch" (a profile with no hard-avoids leaves *selection* unchanged, and the row is hidden
  if the filter thins it below the row minimum). No taste centroid (cold start) → `confidence` is
  null → the UI's neutral "worth a look", so the cold-start experience doesn't regress.
- `continue_watching` / `next_up` — in-progress shows, next episodes (TV roll-up).

Each item carries a per-item **confidence** [0,1] that the UI renders as a **continuous-fill gauge**
(lot R6a): a slim track whose fill width is proportional to the confidence and whose colour is keyed
to the fit bucket (saffron/success ≥ 0.72, neutral below). **No number and no inline text** — a
percentage would be false precision on a heuristic blend (*honesty over engagement*). The worded
bucket label (`common:fit`) still reaches screen readers via the gauge's `aria-label` + hover title,
and shows visibly in the **detail sheet**. **Swings** keep a categorical badge treatment instead of a
gauge — a swing is a deliberate stretch, not a point on the confidence axis. In degraded (local-hash)
mode the gauge tone is capped to neutral so an approximate pick can't read as a confident success.
The chip **must discriminate** — a gauge that reads "strong fit" for every card is not information
(lot R2, owner complaint: every home-row item at 3/3). Confidence is a weighted blend of the
per-title signals that actually vary across a slate:

- **pool-relative similarity** (0.55) — where the pick sits among that query's candidates, *not*
  the raw cosine, which is compressed near the top and would read "strong fit" for everything (H2/A8);
- **absolute pool-strength** (0.20) — the pick's absolute similarity rescaled over the embedder's
  observed band, so "top of a weak pool" reads lower than "top of a strong pool" (pool-relative
  alone can't tell them apart — every pool has a #1);
- **affinity** (0.25) — does it hit a genre they like (the steering signal); only counted when a
  taste profile exists. With no taste, the two similarity terms carry it alone.

Taste-confidence is **not** blended into the mean — a per-profile constant added to every card lifts
the whole slate equally while informing nothing per-title (pure spread compression). It survives
only as a **cap**: a lightly-evidenced profile can't produce a blanket "strong fit". A title with
almost no votes (a just-dropped release, no quality signal yet) is capped too, so it can surface as
a discovery pick but never as a sure thing (review A9) — it graduates on its own as it accrues votes.

The frontend chip buckets that confidence into **strong fit** (≥ 0.72), **worth a try** (≥ 0.45),
and **long shot** (below), plus swings' own "a stretch". Those cuts are calibrated so a realistic
home-row slate lands in at least two buckets with no bucket above ~60% — the eval harness enforces
this with an **anti-uniformity guardrail** (fails a slate whose displayed items all share one
bucket, on the real embedder, skipped offline and for tiny slates). The thresholds live once in the
reranker (`_FIT_STRONG` / `_FIT_TRY`) and are mirrored in `frontend/src/lib/fit.ts`. The heuristic
rows expose an honest signal of their *own* kind, not a fake taste score:

- `watch_again` → your own rating (rating ÷ 10), so the row reads "strong" because it's literally
  your top-rated titles, sorted best-first. A show you're partway through is **excluded** here —
  it belongs in `continue_watching`, and the same series in both rows is a visible contradiction
  (review A11).
- `continue_watching` → recency decay on the last episode you watched (half-life ~6 weeks): a fresh
  thread reads strong, an abandoned one cools off.

Items the profile has already watched carry a `watched` flag, so the UI badges them "Watched" — most
visibly in **search**, where a title you've seen can turn up and should say so (review A11).

Because `continue_watching`'s confidence is recency warmth ("how far into the show you are") and not
a *taste-fit* signal, the UI **doesn't render the fit gauge** on that row — the same gauge meaning
"how far in" on one row and "how well it fits you" on another would make one control mean two things
(review H8). `popular` used to be hidden for the same reason (its old confidence was popularity
magnitude), but since R6b its confidence *is* a real taste fit, so it now **shows the gauge** like the
taste-driven rows (`you_might_like`, `because…`, dynamic themes). `continue_watching` is the sole
remaining member of the frontend's `NO_FIT_ROWS`.

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

## Onboarding (cold start)

A fresh account has no history, so the app shows a first-run takeover (`ColdStart`) until the
profile has any watch events, then reveals the tabbed shell. Browse is gated on
`readyToBrowse` from `GET /profiles/{id}/onboarding` (catalog + history both present); taste
finishes in the background and Browse handles the thin `profile_building` profile honestly.

Three ways in:

- **Connect a library** — Trakt / Plex / Jellyfin import real watch history (the headline path).
- **Start without history** (`start-from-scratch`) — always available, and the *only* path that
  works with no library to connect and no dev sample data. The user searches the catalog and picks
  a handful of titles they loved; each pick logs a `loved` signal (a `watched`+`liked` event pair,
  via the shared feedback endpoint), then a taste generation kicks off and the seeded events unblock
  Browse. Honest by design: the profile is thin (Browse shows `profile_building`), and we encourage
  ~3 picks but proceed from 1.
- **Explore with sample data** — dev-only escape hatch, hidden in production (the sample endpoints
  403 there).

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
