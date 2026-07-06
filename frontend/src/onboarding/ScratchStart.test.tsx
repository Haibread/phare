import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RecommendationItem } from "../api";
import { ScratchStart } from "./ScratchStart";

vi.mock("../api", () => ({
  api: {
    searchCatalog: vi.fn(),
    sendTitleFeedback: vi.fn(),
    generateTaste: vi.fn(),
  },
}));

import { api } from "../api";

const mocked = vi.mocked(api);

afterEach(() => vi.clearAllMocks());

function result(over: Partial<RecommendationItem>): RecommendationItem {
  return {
    titleId: crypto.randomUUID(),
    title: "Arrival",
    kind: "movie",
    year: 2016,
    genres: [],
    score: 0,
    isSwing: false,
    confidence: null,
    explanation: null,
    posterUrl: null,
    components: {},
    watched: false,
    ...over,
  };
}

function tree(onDone = () => {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <ScratchStart profileId="p1" onDone={onDone} />
    </QueryClientProvider>
  );
}

async function searchAndPick(title: string, titleId: string): Promise<void> {
  fireEvent.change(screen.getByTestId("scratch-search"), { target: { value: title } });
  const pick = await screen.findByTestId("scratch-result");
  expect(pick).toHaveTextContent(title);
  void titleId;
  fireEvent.click(pick);
}

describe("ScratchStart", () => {
  it("disables the go button until at least one title is picked", async () => {
    mocked.searchCatalog.mockResolvedValue({ results: [] });
    render(tree());
    expect(screen.getByTestId("scratch-go")).toBeDisabled();
  });

  it("picks titles as removable chips and hides picked results from the list", async () => {
    const inception = result({ title: "Inception", year: 2010, titleId: "t-inc" });
    mocked.searchCatalog.mockResolvedValue({ results: [inception] });

    render(tree());
    await searchAndPick("Inception", "t-inc");

    // The pick shows as a chip and the go button unlocks.
    expect(screen.getByTestId("scratch-pick")).toHaveTextContent("Inception");
    expect(screen.getByTestId("scratch-go")).not.toBeDisabled();

    // Removing the chip clears it and re-locks the button.
    fireEvent.click(screen.getByTestId("scratch-remove"));
    expect(screen.queryByTestId("scratch-pick")).not.toBeInTheDocument();
    expect(screen.getByTestId("scratch-go")).toBeDisabled();
  });

  it("logs each pick as loved, generates taste, then calls onDone", async () => {
    const inception = result({ title: "Inception", titleId: "t-inc" });
    mocked.searchCatalog.mockResolvedValue({ results: [inception] });
    mocked.sendTitleFeedback.mockResolvedValue({
      titleId: "t-inc",
      signal: "loved",
      undoToken: "event:1,event:2",
    });
    mocked.generateTaste.mockResolvedValue({
      profileId: "p1",
      summary: null,
      structured: {},
      userOverrides: {},
      confidence: null,
      modelVersion: null,
      generatedAt: null,
    });
    const onDone = vi.fn();

    render(tree(onDone));
    await searchAndPick("Inception", "t-inc");
    fireEvent.click(screen.getByTestId("scratch-go"));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    // The pick was logged with the `loved` signal, then taste generation ran.
    expect(mocked.sendTitleFeedback).toHaveBeenCalledWith("p1", "t-inc", "loved");
    expect(mocked.generateTaste).toHaveBeenCalledWith("p1");
  });

  it("still lands (onDone) when taste generation fails offline", async () => {
    const inception = result({ title: "Inception", titleId: "t-inc" });
    mocked.searchCatalog.mockResolvedValue({ results: [inception] });
    mocked.sendTitleFeedback.mockResolvedValue({
      titleId: "t-inc",
      signal: "loved",
      undoToken: "event:1",
    });
    // Offline: generate 503s — the seeded events already unblock Browse, so we proceed anyway.
    mocked.generateTaste.mockRejectedValue(new Error("llm unavailable"));
    const onDone = vi.fn();

    render(tree(onDone));
    await searchAndPick("Inception", "t-inc");
    fireEvent.click(screen.getByTestId("scratch-go"));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
  });
});
