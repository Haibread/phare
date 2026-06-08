import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HistoryTable } from "./App";
import type { HistoryItem } from "./api";

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
