# Phare frontend

React + Vite + TypeScript SPA (client-rendered). Talks to the backend API; all logic and
secrets live in the backend. Follows the frontend-conventions skill (TS strict, Biome, zod
at I/O boundaries, Vitest, mobile-first).

## Run it

```bash
# 1. Backend + database
docker compose up -d db
cd backend && uv run phare migrate && uv run phare serve   # serves on :8000

# 2. Frontend (in another terminal)
cd frontend && npm install && npm run dev                  # serves on :5173
```

Open http://localhost:5173, create a profile, click **Load sample data** (no external keys
needed), and the history table fills in. To pull a real history, paste a **Trakt access token**
and click *Sync from Trakt* (requires `TMDB_API_KEY` + `TRAKT_CLIENT_ID` set in the backend env).

`VITE_API_BASE_URL` overrides the backend URL (defaults to `http://localhost:8000`).

## Checks

```bash
npm run typecheck   # tsc --noEmit (strict)
npm run lint        # biome
npm run test        # vitest
npm run build       # tsc + vite build
```
