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
  if (confidence >= 0.66 && !degraded) {
    return { labelKey: "fit.strong", tone: "success", filled: 3 };
  }
  if (confidence >= 0.4) {
    // Includes a would-be "strong" pick in degraded mode — capped to "worth a try", never the top.
    return { labelKey: "fit.worthATry", tone: "neutral", filled: 2 };
  }
  return { labelKey: "fit.longShot", tone: "neutral", filled: 1 };
}
