"""The chat agent: ephemeral mood/intent applied as filters/boosts over the same engine.

Stable taste -> UI rows (the ``recommend`` package). Ephemeral intent ("tired, 90 min, funny")
-> this agent. Same retrieval + re-ranker; the agent only translates a message into extra
filters and writes the reply. Guardrails: per-profile isolation and no cross-user come from the
engine; spoiler-safety on the composed reply is enforced by an explicit prompt instruction plus a
marker post-check (EN + FR) on the blocking path — the streaming path relies on the prompt, since
it emits before the full text exists (review B7).
"""
