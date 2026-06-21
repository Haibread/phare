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
      // SECRET_KEY is required for the now-mandatory multi-user auth (tokens). The specs create the
      // first account (which becomes admin) and sign in — see tests/helpers.ts.
      env: { MIGRATE_ON_STARTUP: "true", SECRET_KEY: "e2e-secret-key" },
    },
    {
      command: "npm --prefix ../frontend run dev",
      url: "http://localhost:5173",
      reuseExistingServer: !isCI,
      timeout: 60_000,
    },
  ],
});
