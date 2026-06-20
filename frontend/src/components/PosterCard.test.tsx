import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type RenderResult, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RecommendationItem } from "../api";
import { PosterCard } from "./PosterCard";
import { RecRow } from "./RecRow";

// Cards embed the (lazy) detail sheet, which uses react-query — provide a client.
function renderCard(ui: React.ReactElement): RenderResult {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function recItem(overrides: Partial<RecommendationItem>): RecommendationItem {
  return {
    titleId: crypto.randomUUID(),
    title: "Arrival",
    kind: "movie",
    year: 2016,
    genres: ["Science Fiction", "Drama"],
    score: 0.9,
    isSwing: false,
    confidence: 0.8,
    explanation: "A cerebral sci-fi that fits your taste.",
    posterUrl: null,
    components: { score: 0.9 },
    ...overrides,
  };
}

describe("PosterCard", () => {
  it("shows the title, year, and a worded fit label", () => {
    renderCard(<PosterCard item={recItem({ title: "Arrival" })} />);
    expect(screen.getAllByText("Arrival").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Strong fit")).toBeInTheDocument();
    expect(screen.getByText(/2016/)).toBeInTheDocument();
  });

  it("badges swing picks and labels them a stretch", () => {
    renderCard(<PosterCard item={recItem({ isSwing: true })} />);
    expect(screen.getByTestId("swing-badge")).toBeInTheDocument();
    expect(screen.getByText("A stretch")).toBeInTheDocument();
  });

  it("renders real poster art when a posterUrl is present", () => {
    // The poster is decorative (alt=""), so query by tag rather than role.
    const { container } = renderCard(
      <PosterCard item={recItem({ posterUrl: "https://img/x.jpg" })} />,
    );
    expect(container.querySelector("img")).toHaveAttribute("src", "https://img/x.jpg");
  });

  it("falls back to the text placeholder without a posterUrl", () => {
    const { container } = renderCard(
      <PosterCard item={recItem({ title: "Moon", posterUrl: null })} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getAllByText("Moon").length).toBeGreaterThanOrEqual(1);
  });
});

describe("RecRow", () => {
  it("renders a card per item under the row title", () => {
    renderCard(
      <RecRow
        row={{
          key: "you_might_like",
          title: "You might like",
          items: [recItem({ title: "Arrival" }), recItem({ title: "Moon" })],
        }}
      />,
    );
    expect(screen.getByText("You might like")).toBeInTheDocument();
    expect(screen.getAllByTestId("rec-card")).toHaveLength(2);
  });

  it("renders nothing for an empty row", () => {
    const { container } = renderCard(
      <RecRow row={{ key: "popular", title: "Popular", items: [] }} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
