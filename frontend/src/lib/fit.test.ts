import { describe, expect, it } from "vitest";
import { fitFor } from "./fit";

describe("fitFor", () => {
  it("treats swings as a stretch regardless of confidence", () => {
    expect(fitFor(0.95, true)).toEqual({ labelKey: "fit.stretch", tone: "swing", filled: 1 });
  });

  it("maps high confidence to a full, strong fit", () => {
    expect(fitFor(0.8, false)).toEqual({ labelKey: "fit.strong", tone: "success", filled: 3 });
  });

  it("maps mid confidence to a two-segment worth-a-try", () => {
    expect(fitFor(0.5, false)).toEqual({ labelKey: "fit.worthATry", tone: "neutral", filled: 2 });
  });

  it("maps low confidence to a single segment", () => {
    expect(fitFor(0.2, false).filled).toBe(1);
  });

  it("applies the recalibrated bucket boundaries (lot R2: 0.72 strong, 0.45 worth-a-try)", () => {
    // Just under "strong" is worth-a-try, not strong — the bar was lifted from 0.66 so the badge
    // discriminates instead of reading 3/3 for everything.
    expect(fitFor(0.71, false).labelKey).toBe("fit.worthATry");
    expect(fitFor(0.72, false).labelKey).toBe("fit.strong");
    // The worth-a-try / long-shot cut sits at 0.45.
    expect(fitFor(0.44, false).labelKey).toBe("fit.longShot");
    expect(fitFor(0.45, false).labelKey).toBe("fit.worthATry");
  });

  it("handles a missing confidence without throwing", () => {
    expect(fitFor(null, false).tone).toBe("neutral");
  });

  it("caps a would-be strong fit to 'worth a try' in offline mode", () => {
    // Local hash embedder: similarity is meaningless, so a high confidence must not read as strong.
    expect(fitFor(0.9, false, true)).toEqual({
      labelKey: "fit.worthATry",
      tone: "neutral",
      filled: 2,
    });
    // A low confidence is unaffected — it was never claiming much.
    expect(fitFor(0.2, false, true).filled).toBe(1);
  });
});
