"""The chat agent: ephemeral mood/intent applied as filters/boosts over the same engine.

Stable taste -> UI rows (the ``recommend`` package). Ephemeral intent ("tired, 90 min, funny")
-> this agent. Same retrieval + re-ranker; the agent only translates a message into extra
filters and writes the reply. Guardrails (per-profile isolation, spoiler-safe, no cross-user)
are inherited from the engine and the explanation layer.
"""
