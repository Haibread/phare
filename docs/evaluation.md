# Evaluation

How we know recommendations are good with no crowd to A/B test.

- **Closed-loop conversion (the north star).** The watch-sync tells us, days later, whether a
  recommended title got watched. Join the recommendation log against later watch events: *of
  titles shown in the top-K, what fraction were watched within N days?* Free, gold-standard
  signal — which is why every rec is logged from day one.
- **Persona guardrail suite (build first, gates CI).** ~6 synthetic personas with known taste
  (horror-fan, rom-com-only, gore-avoider, …) and deterministic assertions (gore-avoider sees no
  gore in top-20; hard-avoids never appear). Catches gross regressions fast. Alongside these
  **alignment** checks assert the relevance *mechanics* a structural test misses (review H7/K1): the
  slate is ordered by score not votes; a free genre key ("sci-fi") actually filters to the catalog's
  "Science Fiction"; and taste affinity varies across the slate instead of reading a flat neutral.
- **`phare evaluate` — the same suite against the *deployed* instance.** CI runs the persona suite
  hermetically with fake providers, so it only protects the *code*. `phare evaluate` runs it against
  your real database with your **configured** embedding/LLM models, and asserts the alignment
  invariants per persona: no `hard_avoids` term in the top-K, affinity varies across the slate, the
  slate is score-descending, and pool-relative similarity spreads (not "strong fit" for everything).
  Each failed check prints its reason next to the persona. This is the only tool that catches a
  relevance regression introduced by a **model or config change** rather than a code change — so run
  it after changing models or upgrading. The similarity-spread check is embedder-specific and is
  skipped (and says so in the output) on the offline `local-hash-v1` embedder the CI harness uses.
  For the same reason an **empty slate** is not failed offline: the local-hash space leaves several
  personas with no neighbours, so an empty slate there is an embedder artifact, not an engine
  regression — but on the real configured embedding space an empty slate *is* a genuine alignment
  failure. The header names both offline skips so a green CI run is never mistaken for having
  exercised them.
- **Temporal holdout (sanity floor, not a target).** Hide each profile's last K watched, check
  if they rank highly (Recall@K / NDCG@K). **Don't over-optimize** — it rewards the obvious and
  is blind to discovery; a perfect score is a popularity machine. Swing slots are excluded from it.
- **Anti-degeneracy metrics.** Popularity bias, catalog coverage, intra-list diversity, novelty —
  these guard against "recommend Shawshank to everyone forever," and are in deliberate tension
  with holdout accuracy. That tension is the product.
- **LLM-as-judge (guardrail, not the grade).** Cheap model scores fit + flags contradictions,
  spoiler leaks, and cross-user references. Noisy → never the sole gate.
