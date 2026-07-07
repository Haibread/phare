import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type RecommendationItem, type RecommendationRow, api } from "../api";
import { ProfileProvider } from "../app/ProfileContext";
import { Hero, pickHero } from "./Browse";

function item(titleId: string, isSwing: boolean): RecommendationItem {
  return {
    titleId,
    title: titleId,
    displayTitle: null,
    kind: "movie",
    year: 2020,
    genres: ["Drama"],
    score: 0.9,
    isSwing,
    confidence: 0.8,
    explanation: null,
    posterUrl: null,
    components: { score: 0.9 },
    watched: false,
    voteAverage: null,
    voteCount: null,
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

describe("Hero", () => {
  afterEach(() => vi.restoreAllMocks());

  function heroItem(): RecommendationItem {
    return { ...item("h1", false), title: "Arrival", explanation: "Sci-fi from 2016." };
  }

  function renderHero(): void {
    render(
      <ProfileProvider value="p1">
        <Hero item={heroItem()} />
      </ProfileProvider>,
    );
  }

  it("shows the template first, then swaps in the streamed personalized why", async () => {
    // Defer the delta to a later tick so the template is what renders on mount, the stream after.
    vi.spyOn(api, "streamTitleExplanation").mockImplementation(async (_p, _t, handlers) => {
      await Promise.resolve();
      handlers.onDelta?.("Because you love cerebral first-contact stories.");
    });

    renderHero();

    // The instant template renders immediately...
    expect(screen.getByTestId("hero-why")).toHaveTextContent("Sci-fi from 2016.");
    // ...then the streamed reason replaces it.
    await waitFor(() =>
      expect(screen.getByTestId("hero-why")).toHaveTextContent(
        "Because you love cerebral first-contact stories.",
      ),
    );
  });

  it("keeps the template when the stream yields nothing (offline / error)", async () => {
    vi.spyOn(api, "streamTitleExplanation").mockRejectedValue(new Error("offline"));
    renderHero();
    await waitFor(() =>
      expect(screen.getByTestId("hero-why")).toHaveTextContent("Sci-fi from 2016."),
    );
  });
});
