# Configuration

All configuration is environment-driven (resolved in
[`core/config.py`](../backend/src/phare/core/config.py); copy `.env.example` to `.env` to set
it). The defaults are chosen so the whole stack runs out of the box with **zero credentials** —
see [Offline / no-key behavior](#offline--no-key-behavior) below for what that actually gives you.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | Free-form environment label. |
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
| `LLM_AGENT_MODEL` | _(falls back to `LLM_CHAT_MODEL`)_ | Optional stronger model for the **conversational chat agent** (planner + natural-language reply). Lets chat use a bigger model while the high-volume mechanical work stays cheap. |
| `LLM_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model. Doubles as the embedding-space version tag. |
| `LLM_EMBEDDING_DIM` | `1536` | Vector dimension. Fixed by the DB schema — changing it needs a migration + full re-embed. |
| `LLM_EMBEDDING_REQUEST_DIMENSIONS` | `false` | Send `LLM_EMBEDDING_DIM` as the `dimensions` request param. Enable for models with configurable (Matryoshka) embeddings so they fit the schema without a re-embed; leave off for models that reject the param. |
| **Metadata + sources** (live syncs/imports only; not needed for the sample-data path) | | |
| `TMDB_API_KEY` | _(unset)_ | TMDB metadata + popular-catalog import + poster art. |
| `TMDB_BASE_URL` / `TMDB_IMAGE_BASE_URL` | TMDB defaults | Override for proxies/mirrors. |
| `TRAKT_CLIENT_ID` | _(unset)_ | Trakt source sync. |
| `TRAKT_CLIENT_SECRET` | _(unset)_ | Also required for the Trakt OAuth device-connect flow. |
| `SEERR_BASE_URL` / `SEERR_API_KEY` | _(unset)_ | Instance-wide Seerr fallback; per-profile creds set in the UI take precedence. |
| **Recommendation tuning** (safe defaults, rarely changed) | | |
| `RECOMMEND_ROW_SIZE` | `12` | Items per row. |
| `RECOMMEND_SWING_SLOTS` | `2` | Reserved high-novelty "swing" picks per slate. |
| **Auth** (opt-in) | | |
| `AUTH_PASSWORD` | _(unset)_ | Set to gate the instance. Unset = open (single-user dev posture). |
| `SECRET_KEY` | falls back to `AUTH_PASSWORD` | Signs bearer tokens **and** derives the source-token encryption key. |
| `AUTH_TOKEN_TTL_SECONDS` | `2592000` (30 days) | Bearer token lifetime. |

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

- **Workhorse** (`LLM_CHAT_MODEL`) — high-volume, mechanical: per-item explanations, taste-extraction
  JSON, dynamic-row naming. Wants a fast, cheap model with reliable JSON. Bigger is *not* better here,
  and pure-reasoning models are worse (they wrap output in thinking traces).
- **Conversational agent** (`LLM_AGENT_MODEL`) — the chat planner and the natural-language reply.
  Tone, nuance, and not mis-hearing you matter, so a stronger *instruct* model earns its keep.

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
