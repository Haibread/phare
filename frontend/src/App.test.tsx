import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HistoryTable, TastePanel } from "./App";
import type { HistoryItem, Taste } from "./api";

function item(overrides: Partial<HistoryItem>): HistoryItem {
  return {
    id: crypto.randomUUID(),
    titleId: crypto.randomUUID(),
    title: "Dune",
    kind: "movie",
    type: "watched",
    rating: null,
    occurredAt: "2024-11-02T20:00:00Z",
    seasonNumber: null,
    episodeNumber: null,
    source: "sample",
    excluded: false,
    ...overrides,
  };
}

describe("HistoryTable", () => {
  it("shows an empty-state message with no items", () => {
    render(<HistoryTable items={[]} />);
    expect(screen.getByText(/no history yet/i)).toBeInTheDocument();
  });

  it("renders a row per item with an episode label for TV", () => {
    render(
      <HistoryTable
        items={[
          item({ title: "Dune" }),
          item({ title: "Severance", kind: "show", seasonNumber: 1, episodeNumber: 2 }),
        ]}
      />,
    );
    expect(screen.getByText("Dune")).toBeInTheDocument();
    expect(screen.getByText(/Severance\s*S1E2/)).toBeInTheDocument();
  });
});

describe("TastePanel", () => {
  it("prompts to generate when there is no taste profile", () => {
    render(<TastePanel taste={null} busy={false} onGenerate={() => {}} />);
    expect(screen.getByText(/no taste profile yet/i)).toBeInTheDocument();
    expect(screen.getByTestId("generate-taste")).toHaveTextContent("Generate taste profile");
  });

  it("renders the summary and likes when present", () => {
    const taste: Taste = {
      profileId: "p1",
      summary: "Likes cerebral sci-fi.",
      structured: { likes: ["slow-burn sci-fi"], hard_avoids: ["gore"] },
      userOverrides: {},
      confidence: 0.7,
      modelVersion: "test",
      generatedAt: "2026-01-01T00:00:00Z",
    };
    render(<TastePanel taste={taste} busy={false} onGenerate={() => {}} />);
    expect(screen.getByText("Likes cerebral sci-fi.")).toBeInTheDocument();
    expect(screen.getByText(/slow-burn sci-fi/)).toBeInTheDocument();
    expect(screen.getByText(/gore/)).toBeInTheDocument();
  });
});
