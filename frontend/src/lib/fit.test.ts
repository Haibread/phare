import { describe, expect, it } from "vitest";
import { fitFor } from "./fit";

describe("fitFor", () => {
  it("treats swings as a stretch with no scalar gauge fill", () => {
    // A swing is categorical (a deliberate stretch), not a point on the confidence axis — so no
    // gauge fill; the card renders the swing badge instead.
    expect(fitFor(0.95, true)).toEqual({ labelKey: "fit.stretch", tone: "swing", fill: null });
  });

  it("maps high confidence to a strong, success-toned gauge filled proportionally", () => {
    expect(fitFor(0.8, false)).toEqual({ labelKey: "fit.strong", tone: "success", fill: 0.8 });
  });

  it("maps mid confidence to a neutral worth-a-try, fill proportional to confidence", () => {
    expect(fitFor(0.5, false)).toEqual({ labelKey: "fit.worthATry", tone: "neutral", fill: 0.5 });
  });

  it("maps low confidence to a long shot with a proportional (but non-empty) fill", () => {
    const fit = fitFor(0.2, false);
    expect(fit.labelKey).toBe("fit.longShot");
    expect(fit.tone).toBe("neutral");
    expect(fit.fill).toBe(0.2);
  });

  it("clamps a genuine-but-tiny fit to a visible sliver, never an empty track", () => {
    // A real 0.02 fit still draws a minimal sliver (0.08) so the gauge doesn't read as "no data".
    expect(fitFor(0.02, false).fill).toBe(0.08);
  });

  it("gives a full fill for a maxed-out confidence", () => {
    expect(fitFor(1, false).fill).toBe(1);
  });

  it("applies the recalibrated bucket boundaries (lot R2: 0.72 strong, 0.45 worth-a-try)", () => {
    // Just under "strong" is worth-a-try, not strong — the bar was lifted from 0.66 so the tone
    // discriminates instead of reading success for everything.
    expect(fitFor(0.71, false).labelKey).toBe("fit.worthATry");
    expect(fitFor(0.72, false).labelKey).toBe("fit.strong");
    // The worth-a-try / long-shot cut sits at 0.45.
    expect(fitFor(0.44, false).labelKey).toBe("fit.longShot");
    expect(fitFor(0.45, false).labelKey).toBe("fit.worthATry");
  });

  it("shows a minimal neutral sliver for a missing confidence (no opinion)", () => {
    const fit = fitFor(null, false);
    expect(fit.tone).toBe("neutral");
    expect(fit.labelKey).toBe("fit.worthALook");
    expect(fit.fill).toBe(0.08);
  });

  it("caps a would-be strong fit to a neutral 'worth a try' tone in offline mode", () => {
    // Local hash embedder: similarity is meaningless, so a high confidence must not read as a
    // success-toned strong fit. The fill still tracks the value; only the tone/bucket is capped.
    expect(fitFor(0.9, false, true)).toEqual({
      labelKey: "fit.worthATry",
      tone: "neutral",
      fill: 0.9,
    });
    // A low confidence is unaffected — it was never claiming much.
    expect(fitFor(0.2, false, true).tone).toBe("neutral");
  });
});
