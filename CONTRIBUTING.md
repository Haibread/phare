# Contributing

Open an [issue](https://github.com/Haibread/phare/issues) to discuss a change, or send a pull
request directly. Vulnerabilities go through [SECURITY.md](SECURITY.md), never a public issue.
The short version: get the setup below working, keep the tests and hooks green, and update the
docs in the same change.

## Development setup

Install:

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which provisions the Python
  version pinned in [`backend/.python-version`](backend/.python-version)
- Node.js 22 and npm (the version CI uses) for `frontend/` and `e2e/`
- Docker with Compose, for the Postgres + pgvector database
- [pre-commit](https://pre-commit.com/#install)
- the two binaries the local hooks shell out to: `actionlint` and `hadolint`
  ([actionlint](https://github.com/rhysd/actionlint/blob/main/docs/install.md),
  [hadolint](https://github.com/hadolint/hadolint#install))

macOS and Linux:

```bash
brew install uv node pre-commit actionlint hadolint
```

On Linux without Homebrew, use your distribution's packages or the install pages above.

From a fresh clone:

```bash
cp .env.example .env
```

```bash
docker compose up -d db
```

```bash
cd backend && uv sync --frozen
```

```bash
cd frontend && npm ci
```

```bash
pre-commit install
```

The setup works when the backend suite below passes. How to run the app itself is in the
[README](README.md#run-it); configuration is in [`docs/configuration.md`](docs/configuration.md).

## Running the tests

The whole backend suite, which needs the `db` service running (the session fixture creates and
drops its own throwaway database on it):

```bash
cd backend && uv run pytest
```

A single file or test:

```bash
cd backend && uv run pytest tests/test_agent.py
```

Set `TEST_DATABASE_URL` to point the suite at another Postgres, and `TEST_DB_NAME` to pick a
stable name for the throwaway database. The suite is hermetic: it blanks LLM and provider
credentials, so a local `.env` cannot leak in and no test calls a real LLM. The few live-LLM
tests are skipped unless `PHARE_LIVE_LLM=1` is set.

The other categories:

| Category | Command | Needs |
| --- | --- | --- |
| Frontend unit | `cd frontend && npm test` | nothing |
| End-to-end (Playwright) | `cd e2e && npm ci && npx playwright install chromium && npm test` | `db` running; starts the backend and the Vite dev server itself |
| Persona evaluation | `cd backend && uv run phare migrate && uv run phare evaluate` | `db` running; see [`docs/evaluation.md`](docs/evaluation.md) |

A new behaviour comes with a test, and a fix with a regression test.

## Pre-commit hooks

[`.pre-commit-config.yaml`](.pre-commit-config.yaml) holds the hooks: ruff and ruff-format for
the backend, hadolint for Dockerfiles, actionlint for workflows, and Biome and `tsc` for the
frontend. They must pass before you push; the same checks run in CI, so skipping them locally
only moves the failure somewhere slower.

```bash
pre-commit run --all-files
```

```bash
pre-commit run <hook-id> --all-files
```

| Hook | Needs | If it fails |
| --- | --- | --- |
| `ruff`, `ruff-format` | nothing | re-run: they fix in place |
| `hadolint` | Docker (the upstream hook runs in a container) | fix the Dockerfile finding |
| `actionlint` | the `actionlint` binary | fix the workflow |
| `frontend-biome`, `frontend-typecheck` | `npm ci` in `frontend/` | `cd frontend && npm run lint:fix`, then fix type errors |

`--no-verify` and `SKIP=` are not the fix: a red hook is a real finding. If a rule is wrong for
this repo, change the configuration in the same PR and say why.

## Continuous integration

[`ci.yml`](.github/workflows/ci.yml) runs on pull requests and on pushes to `main`. It needs no
secrets.

| Job | Purpose | Reproduce locally |
| --- | --- | --- |
| `backend` | ruff lint and format check, backend tests | `cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest -q` |
| `evaluation` | persona guardrails and anti-degeneracy metrics | `cd backend && uv run phare migrate && uv run phare evaluate` |
| `frontend` | Biome, typecheck, unit tests, build | `cd frontend && npx biome ci . && npm run typecheck && npm test && npm run build` |
| `e2e` | Playwright journeys against the full stack | see the end-to-end row above |

Every job must be green to merge. The pre-commit hooks are not run in CI; run them locally.

## Submitting a change

- Branch from `main`, with a short kebab-case name.
- Commit subjects are short, specific and imperative, as in `git log --oneline`.
- Open the PR against `main` with CI green and the documentation updated in the same PR
  ([`CLAUDE.md`](CLAUDE.md) lists the docs and the rules a change must respect).
