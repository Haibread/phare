"""Evaluation: how we know recommendations are good with no crowd to A/B test.

See ``docs/evaluation.md``. Three offline pillars, all runnable in CI without credentials:
persona guardrails (deterministic assertions), temporal holdout (a sanity floor), and
anti-degeneracy metrics (in deliberate tension with holdout accuracy). The LLM-judge is an
optional extra, skipped without a key.
"""
