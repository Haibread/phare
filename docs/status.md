# Status

A compact snapshot of what's built and what's next. Update as features land.

## Built (verified)

- **Backend skeleton** — FastAPI app factory, pydantic-settings config, structured JSON
  logging, OpenTelemetry (traces/metrics/logs over OTLP, auto-off without an endpoint),
  Typer CLI (`serve` / `migrate`), non-root Docker image, docker-compose (pgvector).
- **Canonical schema** (Alembic) — `title → season → episode` tree, `profile`, normalized
  `watch_event` (dedup constraint + index); `pgvector` extension enabled.
- **Ingestion** — provider interfaces (`source`/`metadata`/`llm`/`action`) + fakes; TMDB
  metadata (canonical-id resolution, IMDb→TMDB); Trakt source (history/ratings/watchlist →
  `RawEvent`, paginated); ingestion service (lazy TV tree, idempotent upsert by
  `(profile, source, external_ref)`, most-recent-wins conflict, import cleanup).
- **API** — `GET /history` (paginated, per-profile), `GET/POST /profiles`, dev
  `POST /profiles/{id}/sample-data`, interim `POST /sources/trakt/sync` (paste token).
- **UI** — React + Vite SPA: profile create/select, "Load sample data", "Sync from Trakt",
  history table (TV S/E labels). zod-validated client, structured logger, mobile-first.
- **Embeddings + taste** — OpenAI-compatible LLM provider + versioned title embedding pipeline
  (pgvector, composite PK); LLM taste extraction → structured, editable taste profile
  (`taste_profile`, sticky `user_overrides`); `GET /profiles/{id}/taste`, `POST .../taste/generate`,
  `PUT .../taste`; UI taste panel.
- **Recommendation engine (M3) + chat agent** — local-hash embedding fallback (offline retrieval,
  no key) behind a single embedding-version source of truth; taste centroid → pgvector candidate
  generation (excludes watched + hard-avoids) → deterministic re-ranker (affinity × similarity,
  MMR genre diversity, popularity cap, reserved **swing** slots) → spoiler-safe explanations
  (LLM or template). Rows `you_might_like` / `watch_again` / `popular` / `continue_watching`;
  chat agent applies parsed mood/intent (LLM or keyword fallback) over the same engine.
  `POST /catalog/{sample,import,embed}`, `GET /profiles/{id}/recommendations`,
  `POST /profiles/{id}/chat`; sample catalog + TMDB popular import; UI rows + chat + swing badges.
- **Recommendation logging + closed-loop conversion** — every row/chat item logged
  (`recommendation_log`); `GET /profiles/{id}/recommendations/log`. The north-star metric joins
  the log against later watch events — *of titles shown in the top-K, the fraction watched within
  N days* — counting only matured impressions and reporting swing picks separately
  (`GET .../recommendations/conversion`, surfaced in the UI).
- **Opt-in auth + token model** — `AUTH_PASSWORD`-gated bearer auth (stateless HMAC), `/auth/login`,
  `/me`; per-profile source tokens encrypted at rest (`source_token`, Fernet from `SECRET_KEY`).
  No-op when unconfigured (open dev posture); SPA shows a login gate only when required.
- **More sources** — Plex + Jellyfin source providers (own history only),
  `POST /sources/{plex,jellyfin}/sync`, reusing stored per-profile tokens.
- **Evaluation (M4)** — `eval/` persona guardrail suite + anti-degeneracy metrics (popularity bias,
  diversity, novelty, coverage, holdout recall); `phare evaluate` CLI + a dedicated CI job; optional
  LLM-judge (skipped without a key).
- **Frontend container** — multi-stage nginx (non-root) image + compose `frontend` service, so
  `docker compose up` runs db + backend (self-migrating) + SPA.
- **Tests/CI** — backend pytest (provider HTTP via MockTransport, real-Postgres engine/rows/auth/
  eval), frontend Vitest, **Playwright E2E** (history + recommendations + chat journeys),
  **GitHub Actions** (backend / evaluation / frontend / e2e jobs).

## Run it

```bash
docker compose up --build                                  # whole stack: db + backend + SPA :8080
# …or for development:
docker compose up -d db
cd backend && uv run phare migrate && uv run phare serve   # :8000
cd frontend && npm install && npm run dev                  # :5173
cd e2e && npm install && npx playwright install chromium && npm test   # E2E
```
Set `MIGRATE_ON_STARTUP=true` to have the backend self-migrate (used by E2E/compose, default in
compose). Runs fully offline without `LLM_API_KEY`; set it to use a real embedding/chat model.

## Next features (in rough order)

1. **Real Trakt OAuth connect flow** — replace the interim paste-token endpoint (token storage +
   `/me` identity now exist to build on).
2. **Dynamic LLM-generated rows** — agent picks the day's rows from taste + mood + calendar.
3. **Availability / action providers** — Radarr/Sonarr/Jellyseerr "request" hand-off (needs a
   product decision; see `docs/design.md` deferred list).

## Known gaps / debt

- Auth is opt-in instance-level (single shared password), not multi-account; Trakt is still a
  paste-token sync, not OAuth.
- Plex/Jellyfin episode→show id mapping depends on what the history payload exposes
  (`SeriesProviderIds` / `grandparentGuids`); unmapped episodes are skipped, not guessed.
- The offline local-hash embedder is for dev/CI only — it gives relative similarity, not semantic
  quality; production wants a real embedding model (and a full re-embed on switch).
- Major Postgres version bumps need a dump/restore, not just an image tag change.
