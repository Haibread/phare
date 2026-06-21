# Phare

Open-source, self-hosted, AI-assisted movie & TV recommendations. Learns your taste from what
you've watched and rated (synced from Trakt first), then recommends through **UI rows** and a
**chat agent**.

Core bet: **the LLM steers, embeddings rank** — classic content-based retrieval does the
recommending; the LLM turns fuzzy human signal into an editable taste profile that steers it and
writes the explanations.

Design lives in [`docs/`](docs/); how to build lives in [`CLAUDE.md`](CLAUDE.md); a running
snapshot of what's built is in [`docs/status.md`](docs/status.md).

## What's here

- **Recommendation engine** — taste centroid → pgvector candidate generation → a deterministic
  re-ranker (taste affinity, genre diversity, popularity cap, reserved **swing** slots) → spoiler-
  safe explanations. Surfaced as `you_might_like` / `watch_again` / `popular` / `continue_watching`
  rows.
- **Chat agent** — ephemeral mood/intent ("something funny under 90 minutes") applied as extra
  filters over the same engine.
- **Runs fully offline** — with no `LLM_API_KEY`, a local hash embedder powers retrieval and
  explanations/chat fall back to deterministic templates, so the whole pipeline works (and is
  tested) with zero credentials.
- **Sources** — Trakt, Plex, and Jellyfin (your own history only); TMDB for metadata + a catalog
  import (`phare import-catalog --scope broad` seeds the lesser-known long tail, not just
  blockbusters). A built-in **sample catalog** lets you try it with no accounts.
- **Bilingual (EN/FR)** — defaults to the browser language, with a switcher in the top-right header
  (saved per browser). The choice flows to the backend via `Accept-Language`, so TMDB metadata, row
  titles, and LLM-generated text (explanations, chat, taste) localise too — see
  [`docs/configuration.md`](docs/configuration.md#languages).
- **Multi-user accounts** — per-user login (email + password, or **Sign in with Plex**), each
  account fully isolated from the others; closed by default. The first account is the admin; Plex
  sign-in is gated by access to the owner's Plex server. Source tokens are encrypted at rest. See
  [`docs/auth.md`](docs/auth.md).
- **Evaluation** — persona guardrails + anti-degeneracy metrics (`phare evaluate`), gating CI.

## Run it

Everything runs offline with no credentials; set `LLM_API_KEY` (and friends) for real
recommendation quality. Full knobs + the no-key fallback behavior: [`docs/configuration.md`](docs/configuration.md).

```bash
cp .env.example .env                       # optional; sensible defaults work out of the box

# One command for the whole stack (db + backend + SPA on http://localhost:8080):
docker compose up --build

# …or run the pieces directly for development:
docker compose up -d db
cd backend && uv run phare migrate && uv run phare serve        # API on :8000
cd frontend && npm install && npm run dev                       # SPA on :5173
```

On first run you'll land on a cold-start screen: **Connect your library** (Trakt, Plex, or
Jellyfin) or **Explore with sample data** to try it offline. Either way you drop into the tabbed
shell — **Browse** (taste-driven rows), **Chat** (tell the agent a mood), and **Profile** (your
editable taste, sources, and history).

## Test it

```bash
cd backend  && uv run ruff check . && uv run pytest      # engine, API, providers, eval, auth
cd frontend && npm run lint && npm run typecheck && npm test
cd e2e      && npm ci && npx playwright install chromium && npm test   # full-stack journeys
cd backend  && uv run phare evaluate                     # persona guardrails + metrics
```
