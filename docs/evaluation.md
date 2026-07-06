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
- **Mixed-taste facet guardrail (round 10).** A dedicated check proves the multi-facet retrieval
  actually earns its keep: a two-mode persona (e.g. horror + comedy) must land items from **both**
  modes in the slate, which a single averaged centroid demonstrably fails (it retrieves the blurry
  mid-space between the modes). The check asserts facets pass *and* the forced single-centroid path
  fails on the same persona — a check that passed both ways would prove nothing. It can't run on the
  offline hash embedder (whose cosine geometry is degenerate — no two modes are ever separated), so
  it uses a small deterministic **two-mode embedder** that places each mode in an orthogonal
  subspace, the way the real embedder separates modes semantically. The two modes carry deliberately
  **asymmetric similarity scales** (one dense, high-cosine and populous; one sparse and
  systematically lower-cosine — the real production geometry, measured live on a 4-facet profile
  that rendered a 10/10 single-mode slate): merging raw cross-facet similarities provably fills the
  whole slate from the dense mode, so this same check also pins the cross-facet normalisation
  fairness fix. `phare evaluate` reports it as the `mixed-taste-facets` line regardless of the
  deployed model.
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
