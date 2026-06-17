"""The recommendation engine: candidate generation -> deterministic re-ranker -> explanations.

LLM steers, embeddings rank. Vector retrieval finds candidates; a pure, deterministic re-ranker
does the steering (taste affinity, recency, anti-degeneracy, swing slots); the LLM only writes
explanations. See ``docs/design.md``.
"""
