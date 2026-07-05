/** Maps a recommendation's confidence + swing flag to an honest, non-numeric fit signal: a worded
 * bucket label (accessibility + detail sheet) plus a continuous gauge fill for the card meter. Pure
 * so it can be unit-tested and reused by card + detail views.
 *
 * Swings are never scored against the others — they're deliberate discovery picks, so they get
 * their own "a stretch" treatment regardless of confidence (design.md: honesty over engagement).
 * A swing carries no gauge fill: its meaning is categorical (a reserved stretch pick), not a scalar
 * on the same axis as the others, so the card renders its badge instead of the gauge. */

export type FitTone = "success" | "neutral" | "swing";

export interface Fit {
  /** i18n key under the `common:fit` namespace; translated where rendered (no English here). */
  labelKey: string;
  tone: FitTone;
  /** Continuous gauge fill in [0, 1] — the confidence itself, proportional. `null` for a swing
   * (rendered as a badge, not a gauge) and clamped to a neutral minimum so a real-but-tiny fit is
   * still a visible sliver rather than an empty track. */
  fill: number | null;
}

/** Fit-chip bucket thresholds. MIRROR of the canonical backend constants ``_FIT_STRONG`` /
 * ``_FIT_TRY`` in ``backend/src/phare/recommend/reranker.py`` — keep the two in sync, the numbers
 * must match (the eval anti-uniformity guardrail reads the backend copy).
 *
 * Calibrated (lot R2) against the recalibrated confidence blend on two live profiles so a displayed
 * home-row slate lands in at least two buckets with no bucket above ~60% (measured: A 5%/55%/39%,
 * B 0%/60%/40% long-shot/worth-a-try/strong). A badge that's constant is not information — the
 * owner's complaint was every card reading 3/3 "strong fit". The bar was lifted from the old
 * 0.66/0.40: the new blend rightly produces high confidence for genuinely strong picks, so "strong"
 * now means a real top pick, not merely above the pool median. */
const STRONG_FIT = 0.72;
const WORTH_A_TRY = 0.45;

/** Smallest gauge fill we ever draw for a non-swing pick, so a genuine-but-low fit still reads as a
 * thin sliver rather than an empty track (an empty gauge looks like "no data", which is a different
 * state — that's `confidence === null`, the neutral "worth a look"). */
const MIN_FILL = 0.08;

/** ``degraded`` = retrieval is running on the local hash embedder (no embedding key), so similarity
 * is not semantically meaningful. In that mode the top "strong fit" bucket is never shown and the
 * gauge tone is capped to neutral — an approximate pick must not read as a confident one (review M2). */
export function fitFor(confidence: number | null, isSwing: boolean, degraded = false): Fit {
  if (isSwing) {
    // Categorical, not scalar — the card shows the swing badge, so no gauge fill.
    return { labelKey: "fit.stretch", tone: "swing", fill: null };
  }
  if (confidence === null) {
    // No opinion: the neutral "worth a look" at a minimal sliver (distinct from a real low fit).
    return { labelKey: "fit.worthALook", tone: "neutral", fill: MIN_FILL };
  }
  const fill = Math.max(MIN_FILL, Math.min(1, confidence));
  if (confidence >= STRONG_FIT && !degraded) {
    return { labelKey: "fit.strong", tone: "success", fill };
  }
  if (confidence >= WORTH_A_TRY) {
    // Includes a would-be "strong" pick in degraded mode — capped to "worth a try", never the top.
    return { labelKey: "fit.worthATry", tone: "neutral", fill };
  }
  return { labelKey: "fit.longShot", tone: "neutral", fill };
}
