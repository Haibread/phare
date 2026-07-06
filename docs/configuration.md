# Configuration

All configuration is environment-driven (resolved in
[`core/config.py`](../backend/src/phare/core/config.py); copy `.env.example` to `.env` to set
it). The defaults are chosen so the whole stack runs out of the box with **zero credentials** —
see [Offline / no-key behavior](#offline--no-key-behavior) below for what that actually gives you.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | Free-form environment label. |
| `DEFAULT_LANGUAGE` | `en` | Language (`en`/`fr`) for localised output when a request sends no `Accept-Language`. The SPA sends one per request; see [Languages](#languages). |
| `LOG_LEVEL` | `INFO` | Log verbosity. |
| `SERVICE_NAME` | `phare-backend` | Service name in logs / telemetry. |
| `MIGRATE_ON_STARTUP` | `false` | Run Alembic upgrade on boot. On for compose/E2E; keep off in prod and migrate explicitly. |
| `DATABASE_URL` | `postgresql+psycopg://phare:phare@localhost:5432/phare` | Postgres (with pgvector). |
| `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Allowed SPA origins (comma-separated **or** JSON list). |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(unset)_ | OTLP endpoint. Unset = no exporter wired (self-contained dev/tests). See [`observability.md`](observability.md) for the `phare.fallback` degradation signal. |
| **LLM + embeddings** | | |
| `LLM_API_KEY` | _(unset)_ | OpenAI-compatible key. **Unset = fully offline fallback** (see below). |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Point at any OpenAI-compatible endpoint (Ollama, LM Studio, etc.). |
| `LLM_TIMEOUT_SECONDS` | `120` | HTTP timeout (seconds) for every LLM request. Generous on purpose: taste extraction over a long history on a **reasoning** workhorse can take well over a minute, and a tighter timeout turns a slow-but-fine call into a spurious 503 `llm_unreachable` the user can't get past. Raise it further for very slow local models. |
| `LLM_CHAT_MODEL` | `gpt-4o-mini` | Workhorse chat/completion model: taste extraction, explanations, dynamic rows. |
| `LLM_AGENT_MODEL` | _(falls back to `LLM_CHAT_MODEL`)_ | Optional stronger model used for **one thing only**: the chat agent's natural-language reply. Everything else (planning, explanations, taste) stays on `LLM_CHAT_MODEL`, so a chat turn makes at most one big-model call. |
| `LLM_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model. Base of the embedding-space version tag; the current embed-document version is folded in as a `#d<n>` suffix on the write tag (see [Embedding document + versioned space cutover](design.md#embedding-document--versioned-space-cutover)). |
| `LLM_EMBEDDING_DIM` | `1536` | Vector dimension. Fixed by the DB schema — changing it needs a migration + full re-embed. |
| `LLM_EMBEDDING_REQUEST_DIMENSIONS` | `false` | Send `LLM_EMBEDDING_DIM` as the `dimensions` request param. Enable for models with configurable (Matryoshka) embeddings so they fit the schema without a re-embed; leave off for models that reject the param. |
| `LLM_REASONING_MODEL` | `false` | Set when the chat/agent model is a **reasoning** model (emits `<think>…</think>` before answering, e.g. Qwen3, DeepSeek-R1). Adds `LLM_REASONING_HEADROOM` tokens to every bounded completion so reasoning doesn't eat the budget and return empty JSON, and strips a leading think block from the streamed reply. See [When a configured model misbehaves](#when-a-configured-model-misbehaves). |
| `LLM_REASONING_HEADROOM` | `4096` | Extra completion tokens granted per call when `LLM_REASONING_MODEL` is on. The default clears every structured path including taste extraction (the largest output); raise it further only if a very verbose reasoner still truncates. |
| `LLM_MONTHLY_TOKEN_BUDGET` | `0` (unlimited) | Circuit breaker on LLM spend (review I2). Token usage is always metered (`phare.llm.tokens` OTel counter + debug log); when this is `>0` and the calendar-month total reaches it, **mechanical** calls (taste, explanations, planning, embeddings) refuse and the deterministic fallbacks take over. Process-global; the per-user split is not yet implemented. |
| **Metadata + sources** (live syncs/imports only; not needed for the sample-data path) | | |
| `TMDB_API_KEY` | _(unset)_ | TMDB metadata + catalog import (popular + broad) + poster art. |
| `TMDB_BASE_URL` / `TMDB_IMAGE_BASE_URL` | TMDB defaults | Override for proxies/mirrors. |
| `TMDB_CACHE_TTL_SECONDS` | `3600` | In-process TTL for cached TMDB metadata/search reads (see [Rate limits & caching](#rate-limits--caching)). `0` disables the cache. |
| `TITLE_LOCALIZATION_TTL_SECONDS` | `2592000` (30 d) | DB-persisted TTL for a title's localized synopsis/genres (per language). A detail view serves the cached copy inside the TTL without hitting TMDB, and falls back to it when TMDB is down. |
| `CATALOG_AUTOSEED` | `true` | On startup, if the candidate pool is empty and `TMDB_API_KEY` is set, seed the catalog in the background (see [Seeding the catalog](#seeding-the-catalog)). Master on/off switch. No-op without a TMDB key. |
| `CATALOG_AUTOSEED_SCOPE` | `auto` | What autoseed pulls: `popular` (light front page), `broad` (deep genre sweep), or `auto` (**broad in production**, popular elsewhere — so a prod box deep-seeds itself with no command). |
| `CATALOG_BROAD_PAGES_PER_GENRE` | `20` | Depth of a broad seed (also the autoseed when its scope is broad). Deeper = more titles + more embedding cost. |
| `CATALOG_BROAD_MIN_VOTE_COUNT` | `50` | Quality floor for the broad seed (titles below this vote count are skipped). |
| `CATALOG_REFRESH_INTERVAL_SECONDS` | `86400` (24 h) | How often a background pass pulls **new/current releases** (trending + now-playing/on-the-air) and embeds them, so the catalog keeps up with new movies/TV. `0` disables it. No-op without a TMDB key. |
| `CATALOG_REFRESH_INITIAL_DELAY_SECONDS` | `300` (5 min) | Delay before the **first** refresh after startup — short (not a full interval) so a box that restarts more often than the interval still refreshes, rather than starving. |
| `CATALOG_REFRESH_PAGES` | `1` | Pages of each freshness list pulled per refresh (≈20 titles/page/kind/list). |
| `TRAKT_CLIENT_ID` | _(unset)_ | Trakt source sync. |
| `TRAKT_CLIENT_SECRET` | _(unset)_ | Also required for the Trakt OAuth device-connect flow. |
| `SEERR_BASE_URL` / `SEERR_API_KEY` | _(unset)_ | Instance-wide Seerr fallback; per-profile creds set in the UI take precedence. |
| **Taste extraction** (cost controls for the auto-refresh) | | |
| `TASTE_MAX_EVENTS` | `150` | Most recent events fed into a taste extraction. Bounds prompt (input-token) size; recency-weighted, so the tail rarely changes the profile. |
| `TASTE_REFRESH_MIN_EVENTS` | `8` | New events since the last generation that force an automatic taste refresh. See [Taste auto-refresh gate](#taste-auto-refresh-gate). |
| `TASTE_REFRESH_MIN_INTERVAL_SECONDS` | `21600` (6 h) | Once the profile is older than this **and** something changed, a smaller trickle of events gets folded in. |
| `TASTE_GENERATE_COOLDOWN_SECONDS` | `600` (10 min) | Per-profile rate limit on the manual `POST /taste/generate` (the "Regenerate" button). It force-regenerates (ignoring the auto-refresh gate) and each run spends a workhorse LLM call, so repeated clicks within the cooldown get `429` + `Retry-After` instead. |
| **Recommendation tuning** (safe defaults, rarely changed) | | |
| `RECOMMEND_ROW_SIZE` | `12` | Items per row. |
| `RECOMMEND_SWING_SLOTS` | `2` | Reserved high-novelty "swing" picks per slate. |
| `RECOMMEND_EXPLANATION_BUDGET` | `0` | Eager LLM "why this" calls per home render. **Default `0`** = rows use the instant template and the LLM reason is generated lazily per title (only when a card's detail is opened). Set `>0` to eagerly explain that many cards per render instead. See [Row explanations](#row-explanations). |
| **Auth** (multi-user, closed by default — see [`auth.md`](auth.md)) | | |
| `SECRET_KEY` | _(required once an account exists)_ | Signs identity-bearing bearer tokens **and** derives the source-token encryption key. |
| `AUTH_TOKEN_TTL_SECONDS` | `2592000` (30 days) | Bearer token lifetime. |
| `REGISTRATION_OPEN` | `false` | When true, anyone may self-register a local account. Default closed (admin-created; Plex sign-in is gated by server membership). |
| `PLEX_CLIENT_IDENTIFIER` | _(derived from `SECRET_KEY`)_ | Stable client id Phare presents to plex.tv for "Sign in with Plex". |
| `PLEX_PRODUCT_NAME` | `Phare` | Product name shown on the Plex auth screen. |
| **Rate limiting** (in-memory sliding window; the app is single-process) | | |
| `RATE_LIMIT_ENABLED` | `true` | Master switch for the request rate limiter (review I1). |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding-window length all the per-window counts below apply over. |
| `RATE_LIMIT_AUTH_PER_WINDOW` | `10` | Max `/auth/login`,`/auth/register`,`/auth/password`,`/auth/admin/*/reset-password` per window, **per IP** (brute-force guard). The Plex device-flow poll is exempt. `0` disables this bucket. |
| `RATE_LIMIT_CHAT_PER_WINDOW` | `20` | Max chat turns (`POST …/chat*`) per window, **per user** (each spends an agent-model call). |
| `RATE_LIMIT_IMPORT_PER_WINDOW` | `10` | Max bulk imports (`/catalog/import`,`/catalog/sample`,`/catalog/embed`, source `…/sync`) per window, **per user**. |

Over-quota requests get `429` + `Retry-After` before reaching a handler; the chat UI shows a "slow
down" message. Keyed per IP for the (unauthenticated) auth endpoints, per authenticated user for the
rest (falling back to IP). In-process only — matched to the single-backend deployment assumption.

The pytest suite sets `RATE_LIMIT_ENABLED=false` (in `tests/conftest.py`) and the Playwright e2e
webServer sets it too (in `e2e/playwright.config.ts`): both share one backend from one IP and
re-authenticate on every case, so the prod-default per-IP auth bucket would otherwise fill up mid-run
and 429 the later logins. The e2e webServer also blanks `LLM_API_KEY`/`TMDB_API_KEY`/`TRAKT_*`/
`SEERR_*` (it reads the repo-root `.env`, and a developer's real keys would otherwise leak in and make
the run hit live providers) so it runs fully offline like CI — the same hermetic guard the pytest
suite applies. All of this is scoped to the test webServer; prod defaults are unaffected.

The first cut gated the whole instance behind one shared `AUTH_PASSWORD` and was open when unset.
That's **removed** — Phare is now multi-user with per-account credentials, real per-user isolation,
and no open mode. See [`auth.md`](auth.md) for the model, the "Sign in with Plex" flow, and how the
first account becomes the admin.

## Connecting sources

Which sources a user can connect depends on what the operator configured. `GET /sources/capabilities`
reports this (`{trakt, plex, jellyfin, seerr}`), and the connect UI **greys out** the sources this
server can't support instead of surfacing a raw config error when clicked:

- **Trakt** needs `TMDB_API_KEY` **and** `TRAKT_CLIENT_ID` + `TRAKT_CLIENT_SECRET` (the OAuth app).
- **Plex** and **Jellyfin** need `TMDB_API_KEY` (every title resolves through TMDB); the user
  supplies their own server URL + token/key at connect time.
- **Seerr** is always offered — it only stores user-supplied credentials and needs no server config.

**Jellyfin** connects with a server URL + API key; the UI then calls `POST /sources/jellyfin/users`
to list that server's users and offers a picker, so the operator never has to paste a raw user GUID.
That call is subject to the same SSRF guard as the sync endpoints (internal URLs are rejected).

### Resuming a failed sync

A history import commits in batches of 100, so it survives a mid-sync failure. If a source (or TMDB)
dies partway through, the sync endpoint answers **HTTP 502** with a structured body
`{"detail": {"code": "sync_partial_failure", "ingested": N}}` — `N` is how many watch events already
landed durably. The UI shows this as *"N titles already imported — re-run and it'll resume where it
left off"* with a **Resume import** button. Re-running is safe: the upsert is idempotent, so events
already imported are skipped, not duplicated. Each partial failure is counted on the
`phare.fallback{component="sync",reason="partial_failure"}` metric.

## Seeding the catalog

The recommender ranks over whatever titles are in the catalog — vector similarity can only surface
a title that's been imported and embedded. A fresh instance has **none** to recommend *from*: the
`title` table is filled only by your imported watch history, and all of that is excluded as "already
watched", so recommendations and chat picks come back empty until a candidate pool exists.

| Path | What it adds | How |
| --- | --- | --- |
| **Auto-seed** | TMDB popular titles, on first startup | automatic (see below) |
| **Sample catalog** | A small offline demo pool (no TMDB needed) | `POST /catalog/sample` (dev only) |

The **sample-data escape hatch** (`POST /catalog/sample` + `POST /profiles/{id}/sample-data`, the
onboarding "Explore with sample data" button) is **disabled in production** — both endpoints return
`403`. So the app doesn't offer a button that fails: `GET /sources/capabilities` reports
`sampleData: false` in production, and the onboarding screen hides the escape hatch when it's off.

| **Popular import** | TMDB's popular front page (blockbusters) | `POST /catalog/import` or `phare import-catalog` |
| **Broad import** | A deep genre sweep — the lesser-known long tail | `phare import-catalog --scope broad` |

**Auto-seed (`CATALOG_AUTOSEED`, default on).** On startup, if the candidate pool is thin (fewer
than ~50 never-watched titles) and `TMDB_API_KEY` is set, Phare pulls a few pages of TMDB's popular
titles and embeds them — in a background thread, so readiness never waits on the network. It's
idempotent (upsert by `tmdb_id`) and best-effort: a failure logs and is swallowed, never crashing
startup. No-op without a TMDB key. Set `CATALOG_AUTOSEED=false` to manage the catalog yourself.

**Auto-broad in production.** Popular alone is just blockbusters — not enough for a real instance. So
the autoseed scope (`CATALOG_AUTOSEED_SCOPE`, default `auto`) resolves to the deep **broad** sweep
when `ENVIRONMENT=production`, and to light popular elsewhere. A fresh production box therefore
deep-seeds itself in the background on first boot — no operator command — while a dev box stays light
and never fans out tens of thousands of TMDB requests. Depth/cost is tuned by
`CATALOG_BROAD_PAGES_PER_GENRE` / `CATALOG_BROAD_MIN_VOTE_COUNT`. It's still gated on a thin pool, so
it runs once and then never re-pulls. (The CLI broad seed below remains for a deliberate one-off; the
auto path is the default delivery.)

**Self-healing a full-but-unembedded pool.** A full pool isn't necessarily a *complete* one: a seed's
embed pass can be cut short by a restart, or you might switch `LLM_EMBEDDING_MODEL` (which makes every
title "missing" a vector for the new space). Since autoseed skips once the pool is full, those gaps
would otherwise linger until a read request or the daily refresh trickled them in. So on the skip
path, if any titles lack a current-version vector, Phare kicks off a background embedding backfill of
the whole backlog at boot — no command, no waiting for traffic. It's the same idempotent, best-effort
embed as the seed paths; the read-path top-up and daily refresh remain as backstops.

**Self-healing missing metadata (runtime, credits, language).** The broad *discover* import carries
only shallow records, so a freshly-seeded catalog is almost entirely `runtime_minutes = NULL` with
no credits and — for older rows — no `original_language`. A "something under 90 minutes" request (or
the engine's SQL-side runtime filter) then filters on a ghost catalog, and the detail sheet has no
"who made it" line. The rating re-pull above can't fix this: these fields need a per-title TMDB
**detail** call (which bundles runtime, votes, credits *and* language in one request via
`append_to_response`), not a discover page. So on the same boot skip path, if more than half the
catalog has a **metadata gap** (`runtime_minutes IS NULL OR original_language IS NULL` — one fetch
fills both, plus credits/votes), Phare schedules a **background** bulk heal that walks the gapped
rows in batches behind a keyset cursor, fetches each title's detail (bounded concurrency, capped at
~30 req/s so the fan-out can't rate-limit TMDB), and fills whatever the row was missing. The gap
predicate deliberately includes language, not just runtime: movies whose runtime was already healed
live would otherwise be skipped and never get credits — a catalog fully runtimed but 0% credits
still triggers one pass. It's off the boot path (so readiness isn't gated on it), idempotent (only
ever fills holes — a NULL scalar or an empty credit array, never clobbers), fires once after a broad
seed and then no-ops as coverage stays healthy, and is a strict no-op without a TMDB key. The
read-path enrichment (a detail sheet opening, or the chat candidate pool on a runtime-capped turn)
remains as a backstop that heals whatever the bulk pass hasn't reached yet — no command either way.

**Staying fresh — new movies & TV.** Seeding is a point-in-time snapshot; new releases keep coming,
and nothing on the read path can surface a title that isn't imported yet. So a background pass runs
every `CATALOG_REFRESH_INTERVAL_SECONDS` (default daily; `0` to disable) and pulls TMDB's **current
content** — this week's *trending* plus what's *now playing* / *on the air* — then embeds it. Those
curated lists (unlike vote-filtered *discover*) carry brand-new releases that have barely any votes
yet, which is exactly what we want to catch. It's idempotent (upsert by `tmdb_id`), best-effort, and
runs in a daemon thread that stops cleanly on shutdown — no cron, no command.

**Broad seed (the recommended real-world seed).** `--scope broad` pages TMDB's *discover* endpoint
per genre, sorted by vote count with a quality floor (`--min-vote-count`, default 50, so titles with
a synopsis and real audience get in while micro-obscure noise stays out). This is what lets a Matrix
fan get *Equilibrium* and not just more blockbusters. At the default depth it pulls **tens of
thousands** of titles in a few minutes; raise `--pages-per-genre` to go deeper. It uses *discover*
(one call per ~20 titles) rather than `popular`'s per-title metadata fan-out, so the request count
stays modest. The command embeds the new titles afterwards by default (`--embed`).

**Runtime is filled in lazily, not at import.** *discover* omits per-title runtime, so broad-imported
titles start with `runtime_minutes = NULL` — which means a "something short" chat request has nothing
to filter on, and even a mainstream title's detail sheet would show no runtime. Rather than slow every
import with a per-title detail fan-out, runtimes are **backfilled on the read path**, and only when
needed, on two paths:

- **Chat runtime cap.** A chat turn that actually asks for a length cap fetches the missing runtimes
  **for that turn's candidate pool** from TMDB (in parallel — the provider's HTTP client is
  thread-safe) before filtering, and persists them. It enriches the exact titles being filtered, not
  a global batch, so the cap bites on the first such turn. The same per-title fetch also carries the
  title's **rating** (`vote_average` / `vote_count`), so it **heals a missing quality signal** at the
  same time (see below) when the row lacks one — one fetch, two repairs. Bounded by
  `READ_RUNTIME_CAP` (a code constant, mirroring the lazy embedding top-up).
- **Title detail open.** Opening a title's detail sheet backfills *that* title's runtime if it's
  still NULL, so the sheet shows it. The synopsis localization on the same request already fills the
  runtime from its own TMDB fetch (a first open is a single round-trip); the dedicated fetch is only
  spent when localization served from its warm cache but the runtime is still missing.

Each fetch is permanent, so the catalog heals as it's used — no command, no manual step. All of it is
best-effort: a TMDB hiccup during a detail open leaves the runtime NULL and the sheet still renders.
Without a `TMDB_API_KEY` (or offline), runtime stays NULL: chat length requests parse but don't
constrain — a still-unknown-runtime pool is kept rather than emptied (flagged
`intent_filter.runtime_cap_unenforced` — see [`agent.md`](agent.md)) — and the detail sheet simply
omits the runtime line.

**The quality floor needs a rating to bite.** The re-ranker demotes poorly-rated titles via a
**quality floor** on TMDB's `vote_average` (a hold-back, never a boost). *discover* does return a
rating, so a broad import records it — but a title with a **NULL** `vote_average` takes *no* penalty
(we never guess a title is bad because TMDB is silent). That's the honest default, but it means a
title with no rating on record rides pure similarity. Ratings are kept current three ways: a
re-import or the background **freshness refresh** refreshes `vote_average` / `vote_count` on titles
it touches; the read-path runtime backfill above heals a NULL rating for the titles it fetches; and
**at boot**, if more than half the catalog lacks a rating (the state a past import bug left behind),
the autoseed skip path re-pulls the import as a bulk metadata refresh
(`catalog.autoseed.rating_heal_*` in the logs) — one-time, self-triggering, no operator command. So
the quality signal fills in as the catalog is imported and used, without a manual pass.

**Embeddings are backfilled off the read path.** A title has to be embedded before it can be
recommended, but the read path never embeds a whole fresh import inline (that could freeze the first
request for minutes against a real embedding API). Instead a render embeds only a tiny inline
*micro-batch* — bounded by both a title count and a wall-clock budget (`READ_EMBED_MICRO_LIMIT` /
`READ_EMBED_TIME_BUDGET_S`, code constants) — for the "almost nothing missing" case, and hands any
larger backlog to a single background task that embeds the rest in batches. Only **one** backfill
runs at a time (an in-process lock — the app is single-process). So right after a big import the
first reads come back fast on a partial catalog, and each subsequent read widens the pool as the
backfill catches up; the profile's own titles embedding is what flips the "building your profile"
state off (see [`design.md`](design.md)). The authoritative unbounded pass is still
`POST /catalog/embed`.

**The detail view's synopsis is cached, per language.** Opening a title's "more info" sheet shows
its synopsis and genres in the request language — which means a live TMDB fetch, ~6 s the first time.
That result is cached in the DB keyed by `(title, language)` for `TITLE_LOCALIZATION_TTL_SECONDS`
(default 30 days): a repeat open inside the TTL serves the stored copy with no TMDB call, and if
TMDB is unreachable the stored copy is served anyway (flagged as a fallback) rather than dropping
back to the wrong-language base text. Pure metadata caching — no LLM involved.

**Taste extraction runs in the background, so onboarding lands fast.** Taste is a derived LLM
artifact (see [`data-model.md`](data-model.md)); regenerating it is the slow part of a first import.
So on ingest — seeding sample data, or connecting a source — the history is committed and the app
reveals as soon as the catalog + history exist, while a single background pass (one per profile,
in-process lock) extracts the taste behind it. `GET /profiles/{id}/onboarding` reports the ordered
readiness (catalog → history → taste) the cold-start screen shows as steps; the "building your
profile" state (see [`design.md`](design.md)) covers the window until taste lands. Offline there's
no LLM pass — the deterministic centroid personalises instead — so that step completes immediately.

**Guard — won't run in dev by accident.** Both the import endpoint and the CLI refuse to run unless
`ENVIRONMENT=production`, **or** you explicitly override (`confirm=true` on the endpoint,
`--confirm` on the CLI). This stops a dev box from fanning out thousands of TMDB requests during a
casual test. The deep `broad` seed lives only on the CLI: a minutes-long import is a job, not a
request, so it belongs off the request path.

```bash
# Deep one-time seed on a production box (imports + embeds):
phare import-catalog --scope broad

# Same on a dev box, deliberately:
ENVIRONMENT=production phare import-catalog --scope broad   # or: --confirm
```

After any import, embeddings are topped up lazily on the read path (bounded) and authoritatively by
`POST /catalog/embed` — see [Switching on a real model](#switching-on-a-real-model).

## Languages

Phare ships in **English and French**. The SPA picks the browser language (overridable via the
top-right switcher) and sends it on every request as `Accept-Language`; the backend resolves that to
a supported language (falling back to `DEFAULT_LANGUAGE`) and localises the text it generates.

What localises with the request language:

- **UI chrome** — all static interface text (frontend i18n).
- **Backend-built strings** — recommendation row titles ("You might like" / "Pourrait vous plaire")
  and the offline explanation templates.
- **Freshly-fetched TMDB metadata** — search results and the title-detail synopsis/genres are
  fetched from TMDB in the request language (so the detail sheet shows a French synopsis).
- **LLM-generated text** — recommendation explanations, the chat reply, the taste **summary**, and
  LLM-picked dynamic row themes are written in the request language. The model is _instructed_ in
  the request language (the English system prompts — which carry the scope/guardrails — are kept and
  a one-line output-language directive is appended), so behaviour and safety wording don't drift.
  Recommendation "why this" blurbs are cached (in-process, and durably in `title_explanation`) keyed
  by `(title, language, taste)` — so a blurb is generated once per reader-language and reused. A
  cheap **language sanity guard** screens the model's output before it's cached: a completion that
  clearly reads as the wrong language (a French reply to an English request, or English-led
  Franglais to a French one) is rejected, the deterministic template is served instead, and a
  `phare.fallback` `wrong_language` signal is emitted — so a weak workhorse can't pin a
  mismatched-language blurb into the cache. The check is a deterministic function-word heuristic (no
  extra LLM call) and tolerates the common case of an English film title inside a French sentence.

**Genre names** (TMDB labels) are display-translated in explanation templates via a static table, so
a French sentence reads "Science-Fiction", not the stored English "Science Fiction". The stored
labels stay English — they key affinity/genre matching against the catalog — so only the _displayed_
string is translated. An unmapped genre falls back to English and emits a `phare.fallback` signal.

The **taste summary and free-form taste chips** are served in the request's language: the first read
in a new language spends **one** workhorse LLM call that translates the summary plus every free-form
chip (`likes` / `dislikes` / off-vocabulary `hard_avoids` / `comfort_axis`) in a single JSON payload,
cached per language on the profile (`summary_by_lang`) so each language costs at most one call per
(re)generation. The cache maps each canonical chip to its display form, so removing a chip never
re-spends a call, and chips the user typed as overrides are shown exactly as typed — they're never
machine-translated. Reading in the language the profile was generated in costs nothing. Offline (no
`LLM_API_KEY`) the stored canonical strings are served unchanged. Profiles translated before chips
localized (summary-only cache) keep their cached summary and spend one call on the chips alone.

The **closed-vocabulary taste chips** (TMDB genres + the controlled affinity descriptors) display in
the UI language via a static front-side table (mirrored from the backend's `recommend/genres.py`);
they're excluded from the LLM translation call, so no chip is ever translated twice. In every case
only the _displayed_ label localises: the stored value stays the canonical key, so overrides survive
a language switch and every edit still writes the canonical key to the backend (the API returns the
canonical values in `structured` and the display forms in a separate `displayTerms` map).

What does **not** localise:

- **Structured taste keys** (likes/dislikes/affinity keys) stay English *in storage* on purpose —
  they key affinity matching against the catalog's English genre names (only their display is
  translated, per above).
- **Catalog metadata already stored** from a previous import keeps the language it was imported in;
  only re-fetched TMDB data honours the current language. Titles, people, and years are never
  translated.
- **Tool notes** in a chat reply (e.g. "couldn't find 'Zxqyt'") are surfaced verbatim; the framing
  around them localises.

## Offline / no-key behavior

`LLM_API_KEY` is the master switch. With it **unset**, Phare makes no LLM/embedding network
calls — the whole pipeline still runs, but on deterministic local substitutes. This is what CI,
the E2E suite, and the sample-data path exercise. It is **dev/demo quality, not the real
product** — good for trying the app and proving the plumbing, not for judging recommendation
quality.

| Capability | With `LLM_API_KEY` set | Unset (offline fallback) |
| --- | --- | --- |
| Title embeddings ("rank" half) | Real embedding model; vectors stamped with the model name | [`LocalHashEmbeddingProvider`](../backend/src/phare/providers/embeddings_local.py) — SHA-256 hash → 1536 floats, stamped `local-hash-v1` |
| Candidate similarity | Semantic neighbours | Hash-collision similarity — internally consistent but **not meaningful** |
| Taste extraction | LLM reads history into a prose profile | Limited; relies on the structured/empty path + your editable overrides |
| Explanations | LLM, spoiler-safe sentence | Deterministic metadata-only template (genres/year/kind) |
| Chat intent | LLM mood/intent parsing | Keyword rules ("funny", "90 min", …) |
| Chat **write path** (register "I saw X", commitments, memory) | Full tool-using agent | **Read-only** — no writes (title resolution needs the model) |
| Dynamic "Today's picks" rows | LLM-named themes | Deterministic calendar + top-genre fallback |

**The UI is honest about it.** In offline mode the recommendations response carries
`embeddingsDegraded: true`, so Browse shows a persistent "offline mode — recommendations are
approximate" banner and **caps the fit label** (a pick can never read "strong fit" when the
similarity behind it is a hash collision). This keeps "runs fully offline" from quietly presenting
pseudo-random picks with the same confidence as real ones (review M2).

The two spaces never mix: local and real vectors carry different model-version tags
([`embeddings/version.py`](../backend/src/phare/embeddings/version.py)) and retrieval only queries
the active one. So you can run offline first, then add a key later. The same tag machinery handles
an embed-**document** change: new vectors are written under a `#d<n>`-suffixed tag while reads keep
serving the previous space until the new one is ≥95% built, then flip automatically — no downtime,
no manual step (see the design doc section linked above).

### When a configured model misbehaves

The structured-JSON steps (taste extraction, chat planning, dynamic-row naming) tolerate a model
that wraps its answer in `<think>…</think>` reasoning, fences it in markdown, or surrounds it with
prose — the JSON is salvaged ([`llm_json.py`](../backend/src/phare/llm_json.py)). If the model
returns nothing parseable anyway (a common failure with **reasoning models** that spend their whole
`max_tokens` budget thinking before answering), each step degrades instead of erroring: planning
falls back to a plain `recommend`, dynamic rows fall back to calendar+genre themes, and taste
extraction falls back to a deterministic genre-frequency profile (low confidence, still editable).

These fallbacks are **no longer silent**: a degraded chat turn shows a "basic mode" note under the
reply (it recommended without registering what you said), and a fallback "Today's picks" carries a
`basic` badge. The matching log lines are `plan_failed` / `dynamic_llm_failed` /
`unparseable_completion`.

Degrading to a deterministic profile applies when the model *answered* but unparseably. A different
case is the model being **unreachable** — a transport/HTTP error (403/429/5xx/network) or a spent
`LLM_MONTHLY_TOKEN_BUDGET`. The automatic taste refresh swallows that quietly (taste is best-effort
on ingest), but the **manual "Regenerate" button** does not: you explicitly asked for an LLM pass,
so it returns `503 {"code": "llm_unavailable"}` and the UI shows a "couldn't reach the AI, try again"
message rather than silently handing back a coarse genre-frequency profile (honesty over a fake
result — principle #4). Your existing profile is left untouched, and the fallback is recorded on the
`phare.fallback` counter (`component=taste_extraction`, `reason=llm_unreachable|budget_exhausted`).

If you're running a reasoning model, set **`LLM_REASONING_MODEL=true`** — it grants the structured
calls enough token headroom to finish thinking *and* emit their JSON, and strips the think block
from the streamed reply, which clears up most of the degradation. Otherwise prefer an
**instruct/non-reasoning** model for `LLM_CHAT_MODEL`.

## Security notes

- **SSRF guard on connect URLs.** The Plex/Jellyfin/Seerr connect endpoints take a `base_url`
  and fetch it server-side, so it's validated first (`core/net.py`): it must be `http(s)`, and
  loopback / link-local / unspecified hosts are rejected — `localhost`, `127.0.0.1`, and the cloud
  metadata IP `169.254.169.254` will 400. **Private LAN ranges (`192.168.x.x`, `10.x.x.x`) are
  allowed on purpose** so you can point at a server on your own network. (A hostname that *resolves*
  to a blocked range isn't caught — that's a known limitation, not an oversight.)
- **Spoiler post-check on explanations.** Explanation prompts never include a title's plot
  (`overview`), and the LLM's one-sentence output is additionally screened (`recommend/explain.py`):
  anything overly long or naming a plot reveal is dropped in favour of the deterministic,
  metadata-only template. It's a cheap heuristic backstop, not a guarantee.

## Rate limits & caching

Every outbound provider call (TMDB, the LLM/embedding endpoint, Seerr, Trakt, Plex, Jellyfin) is
wrapped in a **bounded 429 retry** ([`providers/http.py`](../backend/src/phare/providers/http.py)):
on a rate-limit response it honours the `Retry-After` header (clamped to 60s) and retries up to a
few times before surfacing the error. The Trakt OAuth *device flow* is the deliberate exception —
there a 429 means "poll slower", a normal state it handles directly, not an error.

**TMDB reads are also cached.** TMDB is the one third-party metadata API serving idempotent reads
(title lookups, search, `find_by_imdb`), so results are held in a small process-wide TTL+LRU cache
shared across requests. This is what stops the chat agent re-hitting TMDB every time it resolves the
same title across turns, and it softens the popular-import fan-out. Tune freshness with
`TMDB_CACHE_TTL_SECONDS` (default 1 hour; `0` disables it). The cache is in-process only — it resets
on restart and isn't shared between replicas, which is fine for a single-user self-hosted instance.

Self-hosted sources (Plex/Jellyfin/Seerr) get the retry but **no cache**: they're your own servers
(they won't rate-limit their owner) and their state is mutable, so a stale read would be worse than
a fresh call. LLM responses aren't cached either — prompts vary, and title embeddings are already
persisted in Postgres.

### Taste auto-refresh gate

Taste is a derived artifact that re-extracts itself from history (a workhorse LLM call) after a
profile's events change — on sync, on a chat write, on undo. A full re-extraction for every
one-episode incremental sync is wasteful, so the **automatic** refresh is gated
([`taste/service.py`](../backend/src/phare/taste/service.py)): it runs only when

- the profile has never been generated (the first extraction always runs), **or**
- at least `TASTE_REFRESH_MIN_EVENTS` new events have landed since the last generation, **or**
- the profile is older than `TASTE_REFRESH_MIN_INTERVAL_SECONDS` *and* at least one event changed
  (so a slow trickle still gets folded in eventually) — but it never re-runs when nothing changed.

The explicit **`POST /profiles/{id}/taste/generate`** (the "regenerate" button) bypasses the gate
entirely — it always runs. The gate only governs the silent, best-effort refresh.

### Off-topic chat is declined for free

The chat planner returns an empty tool plan for an off-topic message (general questions, coding,
chit-chat, prompt-injection probing). When it does, the turn is answered with a deterministic
steer-back template — **no agent-model call** ([`agent/service.py`](../backend/src/phare/agent/service.py)).
So the expensive tier is spent only on turns that actually recommend or confirm an action, and the
prompt-injection-probe path costs nothing on the big model.

### Output length is capped per call

Every LLM call sends a `max_tokens` bound sized to its job — a one-sentence explanation, tool-plan
JSON, taste JSON, a 1–3 sentence reply ([`providers/llm.py`](../backend/src/phare/providers/llm.py)).
The outputs are short by construction, so this trims tokens you'd otherwise pay for and then
discard (the explanation spoiler-check already drops anything that overruns), and it caps the blast
radius of a misbehaving model.

### Row explanations — lazy by default

The home screen shows ~50 items across rows, but a card's "why this fits you" reason is only ever
*seen* when the user opens that card's detail sheet. So by default Phare **doesn't spend an LLM call
to explain cards nobody opens**:

- **Rows render on the instant template** (genres/year/fit), spoiler-proof by construction — zero
  LLM calls, so the home page never waits on a model.
- **The LLM reason is generated lazily, per title.** When a detail sheet opens, the frontend calls
  `GET /profiles/{id}/titles/{titleId}/explanation`, which generates one workhorse-model reason
  ([`recommend/explain.py`](../backend/src/phare/recommend/explain.py)), spoiler-checks it, and
  **caches** it (keyed by `(title, request language, taste summary)`) so re-opening is free. The
  cache is two-tier: an in-process layer over a durable `title_explanation` Postgres row, so an
  accepted reason **survives restarts and replicas** — it's generated once per taste version, not
  re-spent every time the process recycles. The **request language is part of the key**: a reason is
  written in the reader's language, so an English and a French reader each get (and cache) their own
  text for the same title — the language that asked first never pins the other. It self-invalidates
  when taste changes (new fingerprint → new row) **and when the explanation prompt itself changes** (a
  prompt-version constant is folded into the fingerprint, so a wording change re-generates fresh
  blurbs on the next open instead of serving stale cached ones). Offline, it returns the template.
  The sheet shows the template immediately and swaps in the richer reason when it arrives. The
  **"top pick tonight" hero** does the same on the Browse page: it renders the template on mount,
  streams the personalised reason in the background (no `because` anchor), and swaps it in — so the
  most premium slot shows a real reason, not the deterministic blurb. Later loads hit the cache and
  swap instantly.
- **The reason is personalised, not a synopsis.** The prompt is fed the viewer's own taste — their
  stated likes and the specific genre affinity this title shares (the concrete reason it scored) —
  and is told to address them as "you" and open from that connection ("Since you lean toward …").
  A title two viewers both see gets a different sentence each; a generic back-of-the-box blurb is a
  prompt failure, not the intended output.
- **Cards opened from a "because you watched X" row anchor on that title.** The frontend passes the
  row's seed (`?because=<titleId>`); when the viewer has actually watched it, the reason opens from
  that concrete link ("Since you loved Dune, …") instead of the abstract taste, and is cached
  separately per anchor. The seed is honoured only if it's in the viewer's own history (so the
  param can't probe arbitrary title-to-title links), and only its name + genres reach the prompt —
  never its plot. Cards from other rows (the hero, you-might-like, chat) carry no anchor and use the
  taste-only reason.
- **Chat** is unaffected: it always templates its picks (the agent's reply already frames them).

Prefer the old behaviour (eagerly explain the top cards on every home render, concurrently, cached)?
Set `RECOMMEND_EXPLANATION_BUDGET` to the number of cards to explain per render. The explainer still
bounds + pools that budget, fires the calls concurrently, salvages over-long-but-marker-free replies
to their first sentence, and caches every outcome — but it costs LLM calls on loads, not on clicks.

### Switching on a real model

1. Set `LLM_API_KEY` (and `LLM_BASE_URL` / `LLM_CHAT_MODEL` / `LLM_EMBEDDING_MODEL` as needed).
2. Re-embed the catalog into the new model's space: `POST /catalog/embed` (or `phare` CLI). The
   embedding-version tag changes with the model, so existing local vectors are simply left behind,
   not reused. This endpoint is the **authoritative, unbounded** embed pass — but it is *optional*:
   the read path heals itself, topping up a bounded batch per request and handing the rest to a
   background backfill, so a fresh import can't make the first page load embed the whole catalog
   inline. While the new space builds, reads keep serving the previous space and flip once it is
   ≥95% embedded (then the old space is reclaimed in the background) — the endpoint just makes it
   finish sooner.
3. If you change `LLM_EMBEDDING_DIM` (different-dimension model), that needs a schema migration in
   addition to the re-embed.

### Choosing models (split by job)

Phare asks the LLM to do two very different things, and you can point them at different models:

- **Workhorse** (`LLM_CHAT_MODEL`) — high-volume, mechanical: chat planning (JSON tool selection),
  per-item explanations, taste-extraction JSON, dynamic-row naming. Wants a fast, cheap model with
  reliable JSON. Bigger is *not* better here, and pure-reasoning models are worse (they wrap output
  in thinking traces).
- **Conversational reply** (`LLM_AGENT_MODEL`) — *only* the chat agent's natural-language reply,
  where tone and nuance matter, so a stronger *instruct* model earns its keep. It's one call per
  chat turn; everything else is the workhorse, to keep cost down.

Phare speaks the OpenAI-compatible API, so point it at any provider (hosted or local) by setting
`LLM_BASE_URL` to that provider's endpoint. Pick a small, fast instruct model for the workhorse and
a stronger one for the agent:

```bash
LLM_BASE_URL=<your provider's OpenAI-compatible base URL>
LLM_CHAT_MODEL=<small fast instruct model>     # workhorse
LLM_AGENT_MODEL=<stronger instruct model>      # conversational chat
LLM_EMBEDDING_MODEL=<embedding model>
LLM_EMBEDDING_DIM=1536
LLM_EMBEDDING_REQUEST_DIMENSIONS=true          # only if the embedding model supports it (below)
```

**Embedding dimension:** the schema stores 1536-d vectors. If your embedding model's native size is
something else, you either change `LLM_EMBEDDING_DIM` + run a migration on the vector column + a full
re-embed, **or** — if the model supports configurable (Matryoshka) dimensions — set
`LLM_EMBEDDING_REQUEST_DIMENSIONS=true` to request 1536 directly and skip the migration.
