/** Maps a recommendation's confidence + swing flag to an honest, non-numeric fit label and a
 * coarse 3-step bar. Pure so it can be unit-tested and reused by card + detail views.
 *
 * Swings are never scored against the others — they're deliberate discovery picks, so they get
 * their own "a stretch" treatment regardless of confidence (design.md: honesty over engagement). */

export type FitTone = "success" | "neutral" | "swing";

export interface Fit {
  label: string;
  tone: FitTone;
  filled: 1 | 2 | 3;
}

export function fitFor(confidence: number | null, isSwing: boolean): Fit {
  if (isSwing) {
    return { label: "A stretch", tone: "swing", filled: 1 };
  }
  if (confidence === null) {
    return { label: "Worth a look", tone: "neutral", filled: 1 };
  }
  if (confidence >= 0.66) {
    return { label: "Strong fit", tone: "success", filled: 3 };
  }
  if (confidence >= 0.4) {
    return { label: "Worth a try", tone: "neutral", filled: 2 };
  }
  return { label: "A long shot", tone: "neutral", filled: 1 };
}
