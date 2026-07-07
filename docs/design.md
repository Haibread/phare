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
  Hard-avoids that resolve to catalog genre labels are excluded in the ANN SQL itself (so a
  genre-heavy avoid can't silently erase the whole fetched pool); the in-memory avoid filter stays
  as the backstop for the title-text/keyword matches SQL can't express.
  - **Multi-facet taste retrieval (round 10).** A profile's taste is rarely one thing — someone
    can love cerebral sci-fi *and* dark action *and* prestige drama. Averaging all their liked-title
    vectors into **one** centroid yields a blurry mid-point that is near *none* of those modes, so
    retrieval fetches generic mid-space titles and the genuinely on-taste picks from each mode lose
    (measured across rounds 5–9; embedding-side fixes were exhausted). So instead of one centroid,
    Phare clusters the positively-weighted watched-title vectors into **1–4 taste facets** (a simple,
    deterministic k-means: farthest-point seeded from a stable title-id ordering, fixed iterations,
    no RNG — pgvector is already approximate, the clustering must not add a second source of
    flicker). `k` is adaptive: it grows only while splitting keeps buying separation (a cohesion
    threshold on each cluster's mean intra-similarity), so a genuinely one-note taste stays `k=1`.
    Each facet gets its own ANN query, deep enough to survive the downstream filters (at least
    `max(2k, 24)` candidates each — depth is cheap, one indexed pgvector scan per facet, and a
    proportional-only split proved to starve the light facets live). Pools merge with dedup,
    keeping each candidate's best similarity.
    - **Cross-facet similarity fairness** (live round-10 finding). Raw cosines are **not
      comparable across facets**: a dense region of the embedding space (mainstream action/SF)
      reads systematically higher raw cosines than a sparser one (prestige drama), so merging raw
      similarities let the dominant mode occupy the whole top of the merged range — the re-ranker's
      pool-relative normalisation then squashed every other mode out, and a 4-facet profile
      rendered a 10/10 single-mode slate. So each facet's pool is normalised *within itself* first
      ("top of facet B" competes fairly with "top of facet A") before merging; the honest raw
      cosine is kept alongside, because the confidence meter's absolute band and swing-slot novelty
      must read the *true* scale, never the normalised one. Each candidate also records which facet
      surfaced it (`facet` in the score breakdown — the engine stays inspectable).
    - **Facet-share guarantee.** The main slate reserves slots per facet **proportional to its
      event mass** (a mode backed by 60 % of the history gets ~60 % of the slate; floor of one slot
      for any facet ≥ 15 %), mirroring how swing slots are reserved: the quota decides membership,
      score + MMR still order. A 0.37/0.25/0.20/0.18 profile can no longer render a 10/0/0/0 slate
      — unless the filters/quality floor genuinely empty a facet, in which case its reservation is
      released and recorded as a `facet_quota_starved` fallback, never silently.
    - The re-ranker, mood nudge, and constraint-aware re-fetch all compose unchanged — mood nudges
      each facet, the re-fetch runs per facet. **Negatives** (abandonment / dislike) are *not*
      clustered: they push the whole taste away, so they ride into the *single* centroid, but a
      per-facet query vector is built from positive signal only. So once a profile splits into ≥2
      facets, the retrieval queries carry no repulsion — that job moves to the re-ranker's
      **negative-repulsion penalty** (below), which acts on every candidate regardless of facet
      count.
    - **k=1 degradation.** A small history (< ~8 positively-weighted titles) or an already-cohesive
      taste collapses to a single facet whose centroid **equals** the historical one — so N=1 and
      single-mode profiles behave exactly as before (principle 5). Rewatch rows and title-anchored
      "because you watched X" rows are single-vector by nature and skip faceting entirely. Facets
      are computed from the vectors — no persistent state, no schema change. The clustering runs
      on numpy (same algorithm, same deterministic first-extremum tie-breaks as the original
      pure-Python loops — held to it by a reference-implementation parity test), and the result is
      cached in-process across requests, keyed on the profile, the embedding space, and a cheap
      change-stamp of the profile's watch events (count + latest ingest) so it invalidates
      naturally on any event write; a short TTL bounds staleness from background embedding
      backfills. A `taste.facets` structured log records `k`, the facet sizes, and each facet's
      mean intra-similarity; `taste.facets.cache` records hits/misses.
    - **Inspectable to the user** (principle 2 — the taste profile is never a black box). The same
      deterministic split is exposed read-only at `GET /profiles/{id}/taste/facets`: each facet
      carries a genre-derived label (top 1–2 genres of its member titles, English catalog terms —
      the client localises), its weight (share of positive event mass, facets sum to 1), its title
      count, and its 3 most centroid-central member titles as exemplars. The Profile page renders
      them as "Facets of your taste" with a weight bar and exemplar posters; a single-facet taste
      returns an empty list and the section hides — one blob facet carries no insight. No LLM call.
  - **Adaptive constraint-aware re-fetch.** The first pass retrieves the nearest-to-centroid slice
    and prunes it with the intent filter *after* the fact. For a taste centred elsewhere than the
    requested genre (a thriller fan asking for a light comedy), almost none of those neighbours
    match, so the filtered pool — and then the runtime cap + relevance floor — collapses to ~1. When
    the filtered pool falls below what the re-ranker needs, Phare re-runs the ANN with the constraint
    pushed into **SQL** (array-overlap on the intent genres resolved to canonical catalog labels,
    plus `runtime_minutes <= cap OR runtime IS NULL`), so it ranks the nearest titles *within the
    matching subspace* instead of hoping the global-nearest slice contained matches. It keeps the
    honest relevance floor — a genuinely thin catalog stays thin, never padded with weak matches.
    A first pass that *looks* full but contains **no genre match** (the intent filter's zero-match
    safety keeps the whole pool) still counts as starved, and a re-fetched pool that genuinely
    matches the genre wins regardless of size — otherwise the fallback pool masked the starvation.
  - **Origin-scoped genres ("anime").** "Anime" is not a TMDB genre but *Animation made in Japan*:
    the word (and its French forms) resolves to a structured constraint — genre `Animation` **and**
    `original_language = 'ja'` — enforced identically in the in-memory intent filter and the SQL
    re-fetch (both read the single mapping in `recommend/genres.py`). Plain "animation" / "dessin
    animé" stays a genre-only ask. A NULL-language title does **not** pass an anime request (honest
    thin slate); only when the catalog has *zero* `original_language` coverage (pre-heal / offline)
    does the ask degrade to plain Animation, recorded as a `anime_language_unknown` fallback —
    never a silent "anime becomes animation".
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
  - **Franchise de-duplication** (round-14 live finding: a chat slate carried both *Rush Hour 2*
    and *Rush Hour 3*). At most one instalment of a franchise per slate — two sequels in twelve
    slots is a wasted pick that genre-diversity MMR can't catch (sequels sit close in embedding
    space, but not close enough to be squashed). There is no franchise id in the data, so the key
    is approximated from the title (drop the subtitle after `:`/` - `, strip trailing instalment
    markers — small arabic numerals, roman numerals, "Part/Vol/Chapter"), and it's **deliberately
    conservative**: a lone short word ("It") never counts as a franchise (so it can't swallow *It
    Follows*), a large trailing number is treated as meaningful, not a sequel index (*Blade Runner
    2049* stays whole), while *Alien*/*Aliens* still fold together. The best-scored instalment is
    kept; the freed slot goes to an unrelated title. Applied before the slate is composed, so it
    covers the home rows, the chat slate, **and** swings uniformly.
  - **Negative-taste repulsion** (round 16). A **negative centroid** — the magnitude-weighted
    average embedding of what the profile pushes away (abandoned / disliked / low-rated) — is built
    alongside the taste centroid. Each candidate carries its cosine to it (`neg_similarity`,
    computed as a second cheap distance in the ANN query), and the re-ranker turns that into a
    bounded score penalty: a title sitting in the disliked neighbourhood is demoted, rescaled over
    the embedder's compressed band so it discriminates (≈0 for the many titles unrelated to the
    dislikes, rising only for the genuinely dislike-adjacent). It's **score-only** — never folded
    into displayed confidence (a pick isn't *less likely to fit you* for resembling a dislike, it's
    just ranked lower) — and transparent in the score breakdown (`neg_penalty`). Because it acts
    post-retrieval on every candidate, it repels regardless of facet count, where the query-vector
    signal can't (see the facet note above). Hard-avoids remain the separate hard *filter*; this is
    the soft, graded push.
- **Swing-for-the-fences:** every slate reserves a few deliberate high-novelty picks, *not*
  judged on accuracy. Discovery is the point; pure accuracy yields a popularity machine.
- **Explanations (LLM):** short, spoiler-safe, never cite another user, express confidence.
  **Grounded** in the title's synopsis + keywords so the model describes real appeal instead of
  inventing aligned qualities, and told the actual fit strength so a weak pick is framed as a
  stretch, not oversold (review H4). The synopsis is for grounding only — the prompt forbids
  retelling plot and the output is spoiler-screened; the offline template never touches it.

Why not LLM-as-ranker: it can't recall a large catalog, hallucinates titles, and is slower and
costlier. It's good at reading fuzzy human signal — so that's all it does.

### Embedding document + versioned space cutover

Every title is embedded from a small **document** built in `embeddings/service.py`
(`build_embedding_text`). **Document v2** composes it as, in order: title, year, genres, keywords,
`Directed by:` (director(s) for a movie / creator(s) for a show), `Cast:` (top billed actors),
`Language:` (the ISO original-language code), then the free-text overview **last**. Ordering is
deliberate — short high-signal facets lead so a long overview can't dilute them when the model
averages tokens; the credit + language lines are what separate an auteur's films, an ensemble, and
**anime ("ja" Animation) from western animation** in vector space. This is the round-9 lever against
the observed compression (production cosine similarities bunching into a ~0.82–0.87 band, barely
separable).

Changing the document must re-embed the catalog, but a re-embed can't stall or degrade live reads.
So the document version is folded into the **space tag** (`embeddings/version.py`): historical
vectors carry the bare model tag (`text-embedding-3-small`) which *is* document v1; new vectors are
written under `f"{model}#d{DOC_VERSION}"` (`…#d2`). The version-tag mismatch is the existing
re-embed trigger — nothing else changes on the write side.

Two tags per request, resolved once (one COUNT, in `api/deps.get_embedder`) and threaded everywhere:

- **Write tag** — always the current-document tag. The inline read-path micro-batch, the background
  backfill, and every import/refresh/CLI embed target it, filling the new space in the background at
  the existing backfill pace.
- **Served (read) tag** — `active_embedding_version()`. Serves the **previous** (bare, doc-v1) tag
  until the new space's coverage (embedded ÷ total titles) reaches **`CUTOVER_COVERAGE = 0.95`**,
  then flips to the write tag and logs `embeddings.space_cutover`. First boot after a document
  change: coverage 0% → reads are byte-identical to before while v2 builds; once ≥95% they flip
  automatically. The centroid, the candidate ANN query, and any live free-text query embedding all
  use this one resolved tag, so **a request never mixes spaces**. The offline `embeddings_degraded`
  / `profile_building` UI state derives from the *served* tag — so while a real-model v2 space
  builds, reads still serve the real-model v1 space and the UI shows nothing degraded.

Once the served tag is the write tag (cutover complete), the superseded space's vectors (~126 MB per
space) are deleted in the background (`embeddings/cleanup.py`, `embeddings.space_cleanup`), guarded
so it never deletes the tag currently being served and never leaves the app with no servable space.
`CUTOVER_COVERAGE` is an internal constant, not a config knob — high enough that the flip is
invisible, low enough that a few permanently-unembeddable rows can't wedge it forever.

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

**Search relevance + fit.** Catalog search ranks by *lexical* relevance first — an exact title
match, then a word-start match, then a mid-word substring — and only **within a lexical tier** does
it break ties by `vote_count` (NULLS LAST). So an obscure exact title still leads over a far
better-known title that merely starts with the query. Two guards keep the junk tail down:

- **Vote-floor demotion.** Within the word-start and substring tiers, matches under 50 votes (or
  with none) are *demoted* below every above-floor match of both tiers — still findable, never on
  top (kills "Bikini Inception" ranking beside the real film). The **exact tier is floor-exempt**:
  typing an exact obscure title always finds it first.
- **Semantic fill.** When the above-floor lexical yield falls short of the result limit, the raw
  query text is embedded (one call, in the request's *served* space) and the remaining slots fill
  with the embedding-nearest catalog titles — so "ghibli" surfaces *Spirited Away*, not only
  documentaries whose title contains the word. Fills sit after the good lexical matches but before
  the demoted junk, clear the same vote floor, and use the same wide-`ef_search` deterministic ANN
  as candidate generation. Offline (no embedding key → the local hash space) the tier is skipped
  entirely and search stays purely lexical.
- **Query translation.** Catalog documents are English, so embedding a non-English query clusters
  by document language instead of meaning ("film spatial triste" pulled French films, not sad space
  movies). When the semantic fill fires on a non-English request (gated on the request's
  `Accept-Language`, never on detecting the query's language), the workhorse LLM first translates
  the query to English — one bounded mechanical call per (query, language), cached in-process for
  hours. English requests, lexical-only searches, and offline mode never pay the call, and any
  translation failure falls back to embedding the raw query (recorded as a `search` fallback) —
  search never breaks on it.

Search cards show a compact **TMDB rating** (`★ 8.4 · 37k`, locale-aware compact count; hidden when
a row has no rating) so a junk namesake is tellable from the real film at a glance — home rows don't
carry it, their signal is the fit gauge. Search results also carry an **honest taste-fit
confidence** — the *same* blend as the popular row
(similarity to the taste centroid + graded affinity, unproven-vote cap included), stamped without
re-ordering — so the UI shows the fit gauge on search too. It degrades to `null` (gauge hidden) with
no taste centroid (cold start) or no embedding for a given title; never a fabricated score.

Search cards also show the **fit gauge** when the backend attaches a per-item `confidence` (the
profile has a taste + embeddings). Against an older backend, or a profile with no taste, `confidence`
is `null` and the gauge is **omitted** on search (rather than the neutral "worth a look" sliver the
home rows show for a null) — a raw catalog search shouldn't imply an affinity the engine can't back.

The **title detail sheet** surfaces the richer TMDB metadata a title carries once it's backfilled:
the star **rating** (`★ 7.8 · 12 400 votes`, locale-formatted; a soft "few ratings" hint appears
under ~200 votes, mirroring the reranker's unproven-cap honesty) and the **credits** (director(s),
top cast). Every one of these fields can be absent on an un-healed row — the metadata backfills in
the background over hours — so each renders only when present and its line simply disappears otherwise.

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
