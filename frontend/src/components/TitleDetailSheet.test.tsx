import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type RenderResult, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type RecommendationItem, api } from "../api";
import { ProfileProvider } from "../app/ProfileContext";
import { TitleDetailSheet } from "./TitleDetailSheet";

function renderSheet(ui: React.ReactElement): RenderResult {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ProfileProvider value="p1">{ui}</ProfileProvider>
    </QueryClientProvider>,
  );
}

function recItem(overrides: Partial<RecommendationItem> = {}): RecommendationItem {
  return {
    titleId: "t1",
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
    watched: false,
    ...overrides,
  };
}

describe("TitleDetailSheet", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows the template explanation, then swaps to the streamed reason", async () => {
    // The stream resolves after firing a delta — the richer LLM text replaces the template.
    vi.spyOn(api, "streamTitleExplanation").mockImplementation(async (_p, _t, handlers) => {
      handlers.onDelta?.("Because you loved tense, cerebral sci-fi.");
      handlers.onDone?.();
    });
    vi.spyOn(api, "titleDetail").mockResolvedValue({
      titleId: "t1",
      title: "Arrival",
      kind: "movie",
      year: 2016,
      runtimeMinutes: 116,
      genres: ["Science Fiction"],
      overview: "A linguist makes first contact.",
      posterUrl: null,
      tmdbUrl: null,
      imdbUrl: null,
    });

    renderSheet(<TitleDetailSheet item={recItem()} open={true} onOpenChange={() => {}} />);

    const why = await screen.findByTestId("detail-why");
    await waitFor(() => expect(why).toHaveTextContent("Because you loved tense, cerebral sci-fi."));
  });

  describe("not interested (moved from the card, round 3)", () => {
    it("sends the signal, confirms in-sheet, and restores on undo", async () => {
      vi.spyOn(api, "streamTitleExplanation").mockResolvedValue();
      vi.spyOn(api, "titleDetail").mockResolvedValue({
        titleId: "t1",
        title: "Arrival",
        kind: "movie",
        year: 2016,
        runtimeMinutes: null,
        genres: [],
        overview: null,
        posterUrl: null,
        tmdbUrl: null,
        imdbUrl: null,
      });
      const send = vi.spyOn(api, "sendTitleFeedback").mockResolvedValue({
        titleId: "t1",
        signal: "not_interested",
        undoToken: "event:e1",
      });
      const undo = vi.spyOn(api, "undoChatAction").mockResolvedValue({ undone: true });

      renderSheet(<TitleDetailSheet item={recItem()} open={true} onOpenChange={() => {}} />);

      // The control lives in the sheet, not on the card.
      fireEvent.click(await screen.findByTestId("detail-not-interested"));
      // In-sheet confirmation appears and the signal is sent.
      expect(screen.getByTestId("detail-removed")).toBeInTheDocument();
      await waitFor(() => expect(send).toHaveBeenCalledWith("p1", "t1", "not_interested"));

      // Undo is enabled once the token arrives, then reverses via the chat undo mechanism.
      const undoBtn = screen.getByTestId("detail-undo");
      await waitFor(() => expect(undoBtn).not.toBeDisabled());
      fireEvent.click(undoBtn);
      await waitFor(() => expect(undo).toHaveBeenCalledWith("p1", "event:e1"));
      // The action button returns; the confirmation is gone (item visible again).
      expect(await screen.findByTestId("detail-not-interested")).toBeInTheDocument();
      expect(screen.queryByTestId("detail-removed")).toBeNull();
    });
  });
});
