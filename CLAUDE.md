# CLAUDE.md

Building **Phare** — self-hosted, AI-assisted movie & TV recommendations. This file + `docs/`
record only what isn't derivable from the code or the project's convention skills. For code
style, layout, and tooling, follow the convention skills; don't expect them duplicated here.

## Principles (don't violate these)

1. **LLM steers, embeddings rank.** Vector search + a deterministic re-ranker do the
   recommending. The LLM only: extracts the taste profile, writes explanations, runs the chat
   agent. It never ranks a catalog or picks from memory.
2. **Taste profile is first-class, inspectable, editable** — and is the agent's long-term memory.
   Never a black box.
3. **Two layers:** stable taste → UI rows; ephemeral mood/intent → chat agent. Same engine.
4. **Honesty over engagement.** Show confidence; reserve deliberate discovery ("swing") slots;
   never optimize for time-on-app.
5. **Degrade gracefully, hard-couple to nothing.** Works at N=1 and N=200, on a cheap cloud
   model or a weak local one, with or without Plex/Jellyfin/*arr.
6. **Strict privacy + spoiler safety.** See [`docs/agent.md`](docs/agent.md).
7. **One account = one user.**
8. **Self-triggering over manual steps — avoid the CLI as a delivery mechanism.** Features should be
   packaged and fire when they're needed — lazy on the read path, in the background, on demand — and
   heal themselves as the app is used, not sit behind a `phare …` command or an operator ritual the
   user has to remember to run. A CLI command is a fallback for genuinely out-of-band jobs (a one-off
   bulk migration/seed), never how a normal user- or operator-facing feature is delivered. When
   something needs data or setup it doesn't have yet, make it acquire it on demand (e.g. the lazy
   runtime/embedding backfill on the read path), don't require a command.

## LLM usage (don't violate)

- **Chat agent is strictly scoped to movie & TV recommendation** and the user's own taste / watch
  history. The planner and composer system prompts say so explicitly; off-topic messages (general
  questions, coding, chit-chat, role-change / prompt-injection attempts) are declined and steered
  back — the agent is never a general-purpose assistant. When you touch those prompts, keep the
  scope + decline language intact.
- **Spend the big model sparingly.** Two model tiers (see [`docs/configuration.md`](docs/configuration.md)):
  the **workhorse** (`LLM_CHAT_MODEL`) for high-volume mechanical work (planning JSON, taste
  extraction, row explanations), and the bigger **agent** model (`LLM_AGENT_MODEL`) for *only* the
  user-facing natural-language reply. A chat turn must stay bounded: **≤1 agent-model call** (the
  reply) + at most a couple of workhorse calls (plan, and one taste refresh if a write happened).
  No per-item LLM calls in chat (explanations are templated there); no loops that fan out LLM calls.
  Before adding any LLM call, ask whether a template or the workhorse can do it.
- **Tests never call a real LLM.** The suite is hermetic — `tests/conftest.py` blanks LLM/provider
  credentials so a developer's `.env` can't leak in (kept honest by a test in `test_config.py`).
  Use `FakeLLMProvider` / dependency overrides. The only live-LLM tests are opt-in and skip unless
  `PHARE_LIVE_LLM=1` is set; never make them run by default or in CI.

## Stack (decided)

- Backend/engine/agent: **Python + FastAPI**.
- Data + vectors: **Postgres + pgvector**.
- LLM + embeddings: **OpenAI-compatible, behind a swappable provider interface**.
- Frontend: **React + Vite SPA, client-rendered (no SSR)** — one backend, secrets only in it.
- Everything external (Trakt, TMDB, LLM, *arr) sits behind a provider interface.
- Observability: **structured logs + OpenTelemetry** (traces & metrics) exported over OTLP to a
  swappable backend.

## Docs

**Document everything user- or operator-facing.** Any behavior someone would need to *know*
to run, configure, or reason about Phare — config knobs and their defaults, fallback behavior,
how a feature actually works, operational caveats — must live somewhere durable: the root
`README.md` or a `*.md` under `docs/`. Not just in code comments or a commit message. Keep it
concise (a few lines is fine), but make sure it exists, so the `docs/` tree can later be rendered
straight into a docs website. When you add or change such behavior, update the docs in the same
change. Example worth getting right: how the app behaves with vs. without an LLM/embedding key —
see [`docs/configuration.md`](docs/configuration.md).

- [`docs/design.md`](docs/design.md) — product & engine: what we build, scope, what's deferred.
- [`docs/data-model.md`](docs/data-model.md) — canonical titles, events, taste profile, TV roll-up.
- [`docs/agent.md`](docs/agent.md) — the chat agent + guardrails.
- [`docs/auth.md`](docs/auth.md) — multi-user accounts, tokens, isolation, Sign in with Plex.
- [`docs/configuration.md`](docs/configuration.md) — env vars, and the offline (no-key) fallback behavior.
- [`docs/evaluation.md`](docs/evaluation.md) — how we know recommendations are good.

If a spec seems wrong or a product decision is missing, **ask** — don't invent behavior.
