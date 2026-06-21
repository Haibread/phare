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
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(unset)_ | OTLP endpoint. Unset = no exporter wired (self-contained dev/tests). |
| **LLM + embeddings** | | |
| `LLM_API_KEY` | _(unset)_ | OpenAI-compatible key. **Unset = fully offline fallback** (see below). |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Point at any OpenAI-compatible endpoint (Ollama, LM Studio, etc.). |
| `LLM_CHAT_MODEL` | `gpt-4o-mini` | Workhorse chat/completion model: taste extraction, explanations, dynamic rows. |
| `LLM_AGENT_MODEL` | _(falls back to `LLM_CHAT_MODEL`)_ | Optional stronger model used for **one thing only**: the chat agent's natural-language reply. Everything else (planning, explanations, taste) stays on `LLM_CHAT_MODEL`, so a chat turn makes at most one big-model call. |
| `LLM_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model. Doubles as the embedding-space version tag. |
| `LLM_EMBEDDING_DIM` | `1536` | Vector dimension. Fixed by the DB schema — changing it needs a migration + full re-embed. |
| `LLM_EMBEDDING_REQUEST_DIMENSIONS` | `false` | Send `LLM_EMBEDDING_DIM` as the `dimensions` request param. Enable for models with configurable (Matryoshka) embeddings so they fit the schema without a re-embed; leave off for models that reject the param. |
| **Metadata + sources** (live syncs/imports only; not needed for the sample-data path) | | |
| `TMDB_API_KEY` | _(unset)_ | TMDB metadata + catalog import (popular + broad) + poster art. |
| `TMDB_BASE_URL` / `TMDB_IMAGE_BASE_URL` | TMDB defaults | Override for proxies/mirrors. |
| `TMDB_CACHE_TTL_SECONDS` | `3600` | In-process TTL for cached TMDB metadata/search reads (see [Rate limits & caching](#rate-limits--caching)). `0` disables the cache. |
| `TRAKT_CLIENT_ID` | _(unset)_ | Trakt source sync. |
| `TRAKT_CLIENT_SECRET` | _(unset)_ | Also required for the Trakt OAuth device-connect flow. |
| `SEERR_BASE_URL` / `SEERR_API_KEY` | _(unset)_ | Instance-wide Seerr fallback; per-profile creds set in the UI take precedence. |
| **Taste extraction** (cost controls for the auto-refresh) | | |
| `TASTE_MAX_EVENTS` | `150` | Most recent events fed into a taste extraction. Bounds prompt (input-token) size; recency-weighted, so the tail rarely changes the profile. |
| `TASTE_REFRESH_MIN_EVENTS` | `8` | New events since the last generation that force an automatic taste refresh. See [Taste auto-refresh gate](#taste-auto-refresh-gate). |
| `TASTE_REFRESH_MIN_INTERVAL_SECONDS` | `21600` (6 h) | Once the profile is older than this **and** something changed, a smaller trickle of events gets folded in. |
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

The first cut gated the whole instance behind one shared `AUTH_PASSWORD` and was open when unset.
That's **removed** — Phare is now multi-user with per-account credentials, real per-user isolation,
and no open mode. See [`auth.md`](auth.md) for the model, the "Sign in with Plex" flow, and how the
first account becomes the admin.

## Seeding the catalog

The recommender ranks over whatever titles are in the catalog — vector similarity can only surface
a title that's been imported and embedded. There are three ways to fill the pool, from smallest to
broadest:

| Path | What it adds | How |
| --- | --- | --- |
| **Sample catalog** | A small offline demo pool (no TMDB needed) | `POST /catalog/sample` (dev only) |
| **Popular import** | TMDB's popular front page (blockbusters) | `POST /catalog/import` or `phare import-catalog` |
| **Broad import** | A deep genre sweep — the lesser-known long tail | `phare import-catalog --scope broad` |

**Broad seed (the recommended real-world seed).** `--scope broad` pages TMDB's *discover* endpoint
per genre, sorted by vote count with a quality floor (`--min-vote-count`, default 50, so titles with
a synopsis and real audience get in while micro-obscure noise stays out). This is what lets a Matrix
fan get *Equilibrium* and not just more blockbusters. At the default depth it pulls **tens of
thousands** of titles in a few minutes; raise `--pages-per-genre` to go deeper. It uses *discover*
(one call per ~20 titles) rather than `popular`'s per-title metadata fan-out, so the request count
stays modest. The command embeds the new titles afterwards by default (`--embed`).

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

What does **not** localise:

- **Structured taste keys** (likes/dislikes/affinity keys) and **dynamic-row genres** stay English
  on purpose — they key affinity/genre matching against the catalog's English genre names. Only the
  taste _summary_ and the row _titles_ localise.
- **Catalog metadata already stored** from a previous import keeps the language it was imported in
  (genre labels in a row can stay English even in a French sentence); only re-fetched TMDB data
  honours the current language. Titles, people, and years are never translated.
- **Tool notes** in a chat reply (e.g. "couldn't find 'Zxqyt'") are surfaced verbatim; the framing
  around them localises.

Because the taste profile is a single stored artifact, its summary is written in whichever language
last triggered a (re)generation — regenerate from the profile screen to set it to your language.

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

The two spaces never mix: local and real vectors carry different model-version tags
([`embeddings/version.py`](../backend/src/phare/embeddings/version.py)) and retrieval only queries
the active one. So you can run offline first, then add a key later.

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
  **caches** it (keyed by `(title, taste summary)`) so re-opening is free. The cache is two-tier: an
  in-process layer over a durable `title_explanation` Postgres row, so an accepted reason **survives
  restarts and replicas** — it's generated once per taste version, not re-spent every time the
  process recycles. It self-invalidates when taste changes (new fingerprint → new row). Offline, it
  returns the template. The sheet shows the template immediately and swaps in the richer reason when
  it arrives.
- **Chat** is unaffected: it always templates its picks (the agent's reply already frames them).

Prefer the old behaviour (eagerly explain the top cards on every home render, concurrently, cached)?
Set `RECOMMEND_EXPLANATION_BUDGET` to the number of cards to explain per render. The explainer still
bounds + pools that budget, fires the calls concurrently, salvages over-long-but-marker-free replies
to their first sentence, and caches every outcome — but it costs LLM calls on loads, not on clicks.

### Switching on a real model

1. Set `LLM_API_KEY` (and `LLM_BASE_URL` / `LLM_CHAT_MODEL` / `LLM_EMBEDDING_MODEL` as needed).
2. Re-embed the catalog into the new model's space: `POST /catalog/embed` (or `phare` CLI). The
   active embedding-version tag changes with the model, so existing local vectors are simply left
   behind, not reused. This endpoint is the **authoritative, unbounded** embed pass — run it after
   any catalog import. The recommendation read path only tops up a bounded batch per request (so a
   fresh import can't make the first page load embed the whole catalog inline) and logs
   `embeddings.deferred` when more remain.
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
