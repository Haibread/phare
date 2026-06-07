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

## Stack (decided)

- Backend/engine/agent: **Python + FastAPI**.
- Data + vectors: **Postgres + pgvector**.
- LLM + embeddings: **OpenAI-compatible, behind a swappable provider interface**.
- Frontend: **React + Vite SPA, client-rendered (no SSR)** — one backend, secrets only in it.
- Everything external (Trakt, TMDB, LLM, *arr) sits behind a provider interface.
- Observability: **structured logs + OpenTelemetry** (traces & metrics) exported over OTLP to a
  swappable backend.

## Docs

- [`docs/design.md`](docs/design.md) — product & engine: what we build, scope, what's deferred.
- [`docs/data-model.md`](docs/data-model.md) — canonical titles, events, taste profile, TV roll-up.
- [`docs/agent.md`](docs/agent.md) — the chat agent + guardrails.
- [`docs/evaluation.md`](docs/evaluation.md) — how we know recommendations are good.

If a spec seems wrong or a product decision is missing, **ask** — don't invent behavior.
