import { defineConfig, devices } from "@playwright/test";

const isCI = Boolean(process.env.CI);

/**
 * Starts the backend (self-migrating) and the frontend dev server, then runs the specs.
 * Requires Postgres reachable at the backend's DATABASE_URL (compose `db`, or a CI service).
 */
export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  forbidOnly: isCI,
  retries: isCI ? 1 : 0,
  // The specs share one backend + database; run them serially so concurrent requests against the
  // single dev server can't race (manifested as transient "Failed to fetch").
  workers: 1,
  reporter: isCI ? "list" : [["list"]],
  use: {
    baseURL: "http://localhost:5173",
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "uv run --directory ../backend phare serve",
      url: "http://localhost:8000/health",
      reuseExistingServer: !isCI,
      timeout: 60_000,
      // The webServer inherits this process's env *and* the backend reads ../.env, so a developer's
      // real credentials would otherwise leak in and make the run non-hermetic (live LLM/embedding/
      // TMDB calls → different, flaky results than CI, which has none of these). The pytest suite
      // guards against exactly this in backend/tests/conftest.py; we mirror it here.
      //
      //  - SECRET_KEY: a fixed value the specs' now-mandatory multi-user auth (tokens) needs; they
      //    create the first account (which becomes admin) and sign in — see tests/helpers.ts.
      //  - LLM_API_KEY / TMDB_API_KEY / TRAKT_* / SEERR_*: blanked so the backend runs fully offline
      //    (deterministic sample catalog + fallback embeddings + templated chat), identical to CI.
      //  - RATE_LIMIT_ENABLED=false: the specs run serially against one backend and re-authenticate
      //    from the same IP on every page load; with the prod default (10 /auth/* per IP per 60s) the
      //    shared bucket fills up partway through the run and later logins get 429'd, so the app never
      //    loads (cold-start/tab-browse time out). Scoped to the e2e webServer — prod defaults stand.
      //
      // See docs/configuration.md.
      env: {
        MIGRATE_ON_STARTUP: "true",
        SECRET_KEY: "e2e-secret-key",
        RATE_LIMIT_ENABLED: "false",
        LLM_API_KEY: "",
        TMDB_API_KEY: "",
        TRAKT_CLIENT_ID: "",
        TRAKT_CLIENT_SECRET: "",
        SEERR_BASE_URL: "",
        SEERR_API_KEY: "",
      },
    },
    {
      command: "npm --prefix ../frontend run dev",
      url: "http://localhost:5173",
      reuseExistingServer: !isCI,
      timeout: 60_000,
    },
  ],
});
