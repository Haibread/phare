import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type RenderResult, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type RecommendationItem, api } from "../api";
import { ProfileProvider } from "../app/ProfileContext";
import enTitle from "../locales/en/title.json";
import frTitle from "../locales/fr/title.json";
import { PosterCard } from "./PosterCard";
import { RecRow } from "./RecRow";

// Cards embed the (lazy) detail sheet, which uses react-query + the active profile — provide both.
function renderCard(ui: React.ReactElement): RenderResult {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ProfileProvider value="p1">{ui}</ProfileProvider>
    </QueryClientProvider>,
  );
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
    watched: false,
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

  it("explains the swing badge via a tooltip and aria-describedby (K3)", () => {
    renderCard(<PosterCard item={recItem({ isSwing: true })} />);
    const badge = screen.getByTestId("swing-badge");
    // Hover tooltip is present...
    expect(badge).toHaveAttribute("title", enTitle.badge.swingHelp);
    // ...and the badge is described (for assistive tech) by an element carrying the same text.
    const describedBy = badge.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    const help = document.getElementById(describedBy as string);
    expect(help).toHaveTextContent(enTitle.badge.swingHelp);
  });

  it("carries the swing explanation in both locales (K3)", () => {
    // FR must not be forgotten — the wire text is localized, not hard-coded.
    expect(enTitle.badge.swingHelp.length).toBeGreaterThan(0);
    expect(frTitle.badge.swingHelp.length).toBeGreaterThan(0);
    expect(frTitle.badge.swingHelp).not.toBe(enTitle.badge.swingHelp);
  });

  it("badges a title the profile has already watched (A11)", () => {
    renderCard(<PosterCard item={recItem({ watched: true })} />);
    expect(screen.getByTestId("watched-badge")).toBeInTheDocument();
    renderCard(<PosterCard item={recItem({ watched: false })} />);
    expect(screen.getAllByTestId("watched-badge")).toHaveLength(1); // only the first card
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

  it("shows a fit label on a taste-driven row", () => {
    renderCard(
      <RecRow row={{ key: "you_might_like", title: "You might like", items: [recItem({})] }} />,
    );
    expect(screen.getByTestId("fit")).toBeInTheDocument();
  });

  it("hides the fit label on the continue-watching row", () => {
    // Affinity isn't the question for something you're already partway through (and its confidence
    // is recency warmth, not fit) — so no worded fit meter there.
    renderCard(
      <RecRow
        row={{ key: "continue_watching", title: "Continue watching", items: [recItem({})] }}
      />,
    );
    expect(screen.queryByTestId("fit")).toBeNull();
  });

  it("hides the fit label on the popular row", () => {
    // H8: the popular row's confidence is popularity magnitude, not taste fit — showing it as the
    // same affinity gauge as a personalised row makes one number mean two different things.
    renderCard(<RecRow row={{ key: "popular", title: "Popular", items: [recItem({})] }} />);
    expect(screen.queryByTestId("fit")).toBeNull();
  });

  describe("because-you-watched anchor", () => {
    afterEach(() => vi.restoreAllMocks());

    function openFirstCardOf(rowKey: string): ReturnType<typeof vi.spyOn> {
      const spy = vi.spyOn(api, "streamTitleExplanation").mockResolvedValue();
      renderCard(
        <RecRow row={{ key: rowKey, title: "row", items: [recItem({ title: "Arrival" })] }} />,
      );
      fireEvent.click(screen.getByTestId("rec-card-open"));
      return spy;
    }

    it("passes the seed title id from a `because:<id>` row key to the explanation call", () => {
      const seed = crypto.randomUUID();
      const spy = openFirstCardOf(`because:${seed}`);
      // 5th arg of streamTitleExplanation is the `because` anchor.
      expect(spy.mock.calls[0][4]).toBe(seed);
    });

    it("passes no anchor for a non-because row", () => {
      const spy = openFirstCardOf("you_might_like");
      expect(spy.mock.calls[0][4]).toBeNull();
    });
  });

  describe("not interested (K2)", () => {
    afterEach(() => vi.restoreAllMocks());

    it("sends the signal, removes the card, and restores it on undo", async () => {
      const item = recItem({ title: "Arrival" });
      const send = vi.spyOn(api, "sendTitleFeedback").mockResolvedValue({
        titleId: item.titleId,
        signal: "not_interested",
        undoToken: "event:e1",
      });
      const undo = vi.spyOn(api, "undoChatAction").mockResolvedValue({ undone: true });

      renderCard(<PosterCard item={item} />);

      // The affordance carries an accessible label and is discreet (a button, not text).
      fireEvent.click(screen.getByTestId("not-interested"));
      // The card leaves its slot for an undo placeholder, and the signal is sent.
      expect(screen.getByTestId("rec-card-removed")).toBeInTheDocument();
      await waitFor(() => expect(send).toHaveBeenCalledWith("p1", item.titleId, "not_interested"));

      // Undo is enabled once the write returns its token, then reverses via the chat mechanism.
      const undoBtn = screen.getByTestId("undo-not-interested");
      await waitFor(() => expect(undoBtn).not.toBeDisabled());
      fireEvent.click(undoBtn);
      await waitFor(() => expect(undo).toHaveBeenCalledWith("p1", "event:e1"));
      // The full card returns.
      expect(await screen.findByTestId("not-interested")).toBeInTheDocument();
      expect(screen.queryByTestId("rec-card-removed")).toBeNull();
    });
  });
});
