import { describe, expect, it } from "vitest";
import type { RecommendationItem, RecommendationRow } from "../api";
import { pickHero } from "./Browse";

function item(titleId: string, isSwing: boolean): RecommendationItem {
  return {
    titleId,
    title: titleId,
    kind: "movie",
    year: 2020,
    genres: ["Drama"],
    score: 0.9,
    isSwing,
    confidence: 0.8,
    explanation: null,
    posterUrl: null,
    components: { score: 0.9 },
  };
}

function row(key: string, items: RecommendationItem[]): RecommendationRow {
  return { key, title: key, items };
}

describe("pickHero", () => {
  it("skips a swing at the top and returns the first non-swing pick (A6)", () => {
    const rows = [row("you_might_like", [item("a", true), item("b", false), item("c", false)])];
    expect(pickHero(rows)?.titleId).toBe("b");
  });

  it("returns no hero when every candidate is a swing", () => {
    const rows = [row("you_might_like", [item("a", true), item("b", true)])];
    expect(pickHero(rows)).toBeUndefined();
  });

  it("falls back to the first row when you_might_like is absent", () => {
    const rows = [row("popular", [item("x", false)])];
    expect(pickHero(rows)?.titleId).toBe("x");
  });
});
