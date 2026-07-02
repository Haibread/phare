# Observability

Phare emits structured logs and OpenTelemetry traces/metrics over OTLP. Exporters are wired only
when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (see [`configuration.md`](configuration.md)); without it
the app runs with no collector and the metrics are cheap no-ops. FastAPI and SQLAlchemy are always
instrumented.

## The fallback signal (`phare.fallback`)

Phare degrades gracefully rather than failing — a genre filter that matches nothing, a flaky LLM
call, a truncated embedding backfill all fall back to something reasonable. The problem that
motivated this (review G1) is that when those fallbacks are **silent**, every kind of trouble looks
the same from the outside: "it answered, but it's mediocre", with nothing to point at.

So every degrade-and-continue path routes through one helper, `core.fallback.record_fallback`, which
emits both a structured warning **and** a counter:

- **Log:** `WARNING` on the `phare.fallback` logger, message `"<component>.fallback"`, with a
  `reason` field plus any extra context (e.g. `wanted`, `title`, `dropped`).
- **Metric:** the counter `phare.fallback`, tagged with attributes `component` and `reason`.

`component` names the brick that degraded, `reason` is the machine-readable cause. Current emitters:

| component | reason(s) | meaning |
|-----------|-----------|---------|
| `genre_filter` | `no_match` | a chat/theme genre filter matched nothing and returned the unfiltered pool |
| `taste_affinity` | `key_unmatched` | a taste affinity key resolves to nothing in the closed vocabulary (can't steer scoring) |
| `taste_extraction` | `unparseable_completion` | the LLM taste profile didn't parse; fell back to the genre-frequency profile |
| `planner` | `malformed_response`, `parse_error` | the chat planner's output was unusable; defaulted to a plain recommend |
| `agent_tool` | `exception`, `unknown_tool` | a chat tool raised, or the planner asked for a tool that doesn't exist |
| `explain` / `explain_stream` | `llm_error`, `spoiler_rejected` | a "why this" blurb fell back to the template (provider error, or the spoiler guard rejected it) |
| `candidates` | `hard_avoids_emptied` | hard-avoids removed every nearby candidate, leaving an empty slate |
| `embeddings` | `backfill_deferred` | the read-path embedding top-up hit its cap; the catalog isn't fully embedded yet |

**How to use it.** Alert on a rising `phare.fallback` rate overall (the app is quietly degrading),
and break down by `component`/`reason` to see *where*. A spike in `genre_filter{no_match}` or
`taste_affinity{key_unmatched}` points at a vocabulary problem; `explain{llm_error}` or
`planner{parse_error}` at a struggling model; `embeddings{backfill_deferred}` at an import that never
finished. When you add a new fallback path, call `record_fallback` — don't `except: pass`.
