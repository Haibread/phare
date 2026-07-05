/** Maps a recommendation's confidence + swing flag to an honest, non-numeric fit label and a
 * coarse 3-step bar. Pure so it can be unit-tested and reused by card + detail views.
 *
 * Swings are never scored against the others — they're deliberate discovery picks, so they get
 * their own "a stretch" treatment regardless of confidence (design.md: honesty over engagement). */

export type FitTone = "success" | "neutral" | "swing";

export interface Fit {
  /** i18n key under the `common:fit` namespace; translated where rendered (no English here). */
  labelKey: string;
  tone: FitTone;
  filled: 1 | 2 | 3;
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

/** ``degraded`` = retrieval is running on the local hash embedder (no embedding key), so similarity
 * is not semantically meaningful. In that mode the top "strong fit" bucket is never shown — an
 * approximate pick must not read as a confident one (review M2). */
export function fitFor(confidence: number | null, isSwing: boolean, degraded = false): Fit {
  if (isSwing) {
    return { labelKey: "fit.stretch", tone: "swing", filled: 1 };
  }
  if (confidence === null) {
    return { labelKey: "fit.worthALook", tone: "neutral", filled: 1 };
  }
  if (confidence >= STRONG_FIT && !degraded) {
    return { labelKey: "fit.strong", tone: "success", filled: 3 };
  }
  if (confidence >= WORTH_A_TRY) {
    // Includes a would-be "strong" pick in degraded mode — capped to "worth a try", never the top.
    return { labelKey: "fit.worthATry", tone: "neutral", filled: 2 };
  }
  return { labelKey: "fit.longShot", tone: "neutral", filled: 1 };
}
