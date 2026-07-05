import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type RenderResult, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type RecommendationItem, api } from "../api";
import { ProfileProvider } from "../app/ProfileContext";
import { type AvailabilityCtx, AvailabilityProvider } from "./Availability";
import { TitleDetailSheet } from "./TitleDetailSheet";

function renderSheet(
  ui: React.ReactElement,
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
): RenderResult {
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

  describe("worded fit label (R6a: the card gauge dropped its inline text)", () => {
    it("shows the bucket wording, toned to match the gauge", () => {
      vi.spyOn(api, "streamTitleExplanation").mockResolvedValue();
      renderSheet(
        <TitleDetailSheet
          item={recItem({ confidence: 0.8 })}
          open={true}
          onOpenChange={() => {}}
        />,
      );
      const fit = screen.getByTestId("detail-fit");
      expect(fit).toHaveTextContent("Strong fit");
      expect(fit).toHaveAttribute("data-tone", "success");
    });

    it("omits the fit label for a row with no taste-fit signal (showFit=false)", () => {
      vi.spyOn(api, "streamTitleExplanation").mockResolvedValue();
      renderSheet(
        <TitleDetailSheet item={recItem()} open={true} onOpenChange={() => {}} showFit={false} />,
      );
      expect(screen.queryByTestId("detail-fit")).toBeNull();
    });
  });

  describe("request / availability action (moved from the card, round 4)", () => {
    function stubDetail() {
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
    }

    function renderWithAvailability(ctx: Partial<AvailabilityCtx>): void {
      const value: AvailabilityCtx = {
        configured: true,
        results: {},
        requestingId: null,
        onRequest: () => {},
        ...ctx,
      };
      renderSheet(
        <AvailabilityProvider value={value}>
          <TitleDetailSheet item={recItem()} open={true} onOpenChange={() => {}} />
        </AvailabilityProvider>,
      );
    }

    it("offers the Request button inside the sheet under a configured provider", async () => {
      stubDetail();
      const onRequest = vi.fn();
      renderWithAvailability({ results: { t1: "requestable" }, onRequest });
      fireEvent.click(await screen.findByTestId("title-request"));
      expect(onRequest).toHaveBeenCalledWith("t1");
    });

    it("renders nothing (gracefully) when there is no availability provider — Browse/chat", () => {
      // Exactly as the card version did: with no provider in the tree, TitleAction is null.
      stubDetail();
      renderSheet(<TitleDetailSheet item={recItem()} open={true} onOpenChange={() => {}} />);
      expect(screen.queryByTestId("title-request")).toBeNull();
      expect(screen.queryByTestId("title-available")).toBeNull();
    });
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

    it("defers the row refresh until the sheet closes, so the undo offer survives", async () => {
      // Regression: the sheet lives inside the row's card. Invalidating the recommendation rows
      // as soon as the feedback lands refetches the rows, drops the title, unmounts the card —
      // and takes the sheet's undo affordance down with it. The refresh must wait for close.
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
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const invalidated = vi.spyOn(qc, "invalidateQueries");
      const rowKeys = () =>
        invalidated.mock.calls.filter((c) => {
          const key = (c[0] as { queryKey: unknown[] }).queryKey;
          return key.includes("recommendations") || key.includes("dynamic");
        });

      const view = renderSheet(
        <TitleDetailSheet item={recItem()} open={true} onOpenChange={() => {}} />,
        qc,
      );
      fireEvent.click(await screen.findByTestId("detail-not-interested"));
      await waitFor(() => expect(send).toHaveBeenCalled());
      // While the sheet is open with the undo on offer, the rows stay untouched.
      expect(rowKeys()).toHaveLength(0);

      // Closing the sheet un-undone flushes the refresh and the title drops from Browse.
      view.rerender(
        <QueryClientProvider client={qc}>
          <ProfileProvider value="p1">
            <TitleDetailSheet item={recItem()} open={false} onOpenChange={() => {}} />
          </ProfileProvider>
        </QueryClientProvider>,
      );
      await waitFor(() => expect(rowKeys().length).toBeGreaterThanOrEqual(2));
    });
  });
});
