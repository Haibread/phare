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
- **Embeddings + taste (on `feat/step3-taste-embeddings`, not yet merged)** — OpenAI-compatible
  LLM provider + versioned title embedding pipeline (pgvector, composite PK); LLM taste
  extraction → structured, editable taste profile (`taste_profile`, sticky `user_overrides`);
  `GET /profiles/{id}/taste`, `POST .../taste/generate`, `PUT .../taste`; UI taste panel.
- **Tests/CI** — backend pytest (incl. provider HTTP via MockTransport, real-Postgres
  ingestion/history/profiles), frontend Vitest, **Playwright E2E** (create → sample-data →
  history), **GitHub Actions** (backend / frontend / e2e jobs).

## Branches / PRs

- `main` — Steps 1 (skeleton) + 2 (ingestion) + UI/tests/CI (PR #4), merged.
- `feat/step3-taste-embeddings` — embeddings + taste extraction + taste UI (ready for PR).

## Run it

```bash
docker compose up -d db
cd backend && uv run phare migrate && uv run phare serve   # :8000
cd frontend && npm install && npm run dev                  # :5173
cd e2e && npm install && npx playwright install chromium && npm test   # E2E
```
Set `MIGRATE_ON_STARTUP=true` to have the backend self-migrate (used by E2E/compose).

## Next features (in rough order)

1. **Recommendation engine (M3)** — candidate generation (vector + filters) → re-ranker
   (profile steering via the taste profile, diversity, swing slots) → explanations; the
   `you_might_like` row. The embeddings + taste profile it needs now exist.
2. **Recommendation logging** — log every rec shown (closed-loop metric groundwork).
3. **Auth / token model (OQ-02)** — unblocks real Trakt OAuth + per-profile token storage and
   gives `/me` a real identity (currently the profile selector stands in).
4. **Recommendation logging** — log every rec shown (closed-loop metric groundwork).
5. **Evaluation (M4)** — persona guardrail suite in CI, temporal holdout, anti-degeneracy
   metrics, LLM-judge.
6. **Frontend container + compose service** — one-command `docker compose up` for the whole app.

## Known gaps / debt

- No real auth yet (single-user dev posture).
- Trakt sync is an interim paste-token endpoint, not OAuth.
- Frontend not containerised (run via `npm run dev`).
- Major Postgres version bumps need a dump/restore, not just an image tag change.
