import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type RenderResult, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type RecommendationItem, api } from "../api";
import { ProfileProvider } from "../app/ProfileContext";
import enTitle from "../locales/en/title.json";
import frTitle from "../locales/fr/title.json";
import { AvailabilityProvider } from "./Availability";
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
    voteAverage: null,
    voteCount: null,
    ...overrides,
  };
}

describe("PosterCard", () => {
  it("shows the title, year, and a continuous-fill fit gauge (no inline text)", () => {
    renderCard(<PosterCard item={recItem({ title: "Arrival", confidence: 0.8 })} />);
    expect(screen.getAllByText("Arrival").length).toBeGreaterThanOrEqual(1);
    // The year appears in the card meta and, on a poster-less card, in the placeholder too — so
    // just assert it's present at least once (fixes 4/6 put the year in the fallback tile).
    expect(screen.getAllByText(/2016/).length).toBeGreaterThanOrEqual(1);
    // The gauge is a meter labelled with the bucket wording (no inline text on the card anymore).
    const gauge = screen.getByTestId("fit");
    expect(gauge).toHaveAttribute("role", "meter");
    expect(gauge).toHaveAttribute("aria-label", "Strong fit");
    // The worded label is NOT rendered as visible card text.
    expect(screen.queryByText("Strong fit")).toBeNull();
  });

  it("fills the gauge proportionally to confidence and tones it by bucket", () => {
    const { container } = renderCard(<PosterCard item={recItem({ confidence: 0.8 })} />);
    const fill = container.querySelector('[data-testid="fit"] > span > span') as HTMLElement;
    expect(fill.style.width).toBe("80%");
    expect(fill.getAttribute("data-tone")).toBe("success");
  });

  it("tones a mid-confidence gauge neutral and fills it less", () => {
    const { container } = renderCard(<PosterCard item={recItem({ confidence: 0.5 })} />);
    const gauge = screen.getByTestId("fit");
    expect(gauge).toHaveAttribute("aria-label", "Worth a try");
    const fill = container.querySelector('[data-testid="fit"] > span > span') as HTMLElement;
    expect(fill.style.width).toBe("50%");
    expect(fill.getAttribute("data-tone")).toBe("neutral");
  });

  it("renders a swing as its badge, not as a gauge", () => {
    renderCard(<PosterCard item={recItem({ isSwing: true })} />);
    // The swing badge carries the categorical meaning; the scalar gauge is suppressed.
    expect(screen.getByTestId("swing-badge")).toBeInTheDocument();
    expect(screen.queryByTestId("fit")).toBeNull();
  });

  it("gives the open button a full accessible name: title, year and fit (L2)", () => {
    renderCard(<PosterCard item={recItem({ title: "Arrival", year: 2016 })} />);
    const open = screen.getByTestId("rec-card-open");
    const label = open.getAttribute("aria-label") ?? "";
    expect(label).toContain("Arrival");
    expect(label).toContain("2016");
    expect(label).toContain("Strong fit");
  });

  it("names the open button without a fit when the meter is hidden (L2)", () => {
    renderCard(<PosterCard item={recItem({ title: "Moon", year: 2009 })} showFit={false} />);
    const label = screen.getByTestId("rec-card-open").getAttribute("aria-label") ?? "";
    expect(label).toContain("Moon");
    expect(label).not.toContain("fit");
  });

  it("badges swing picks (the categorical treatment, in place of the scalar gauge)", () => {
    renderCard(<PosterCard item={recItem({ isSwing: true })} />);
    // A swing keeps its distinct badge treatment; the worded "A stretch" wording survives in the
    // gauge/detail-sheet path, not as inline card text — the card no longer renders any fit words.
    expect(screen.getByTestId("swing-badge")).toBeInTheDocument();
    expect(screen.queryByTestId("fit")).toBeNull();
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

  it("falls back to an honest title+year placeholder without a posterUrl", () => {
    const { container } = renderCard(
      <PosterCard item={recItem({ title: "Moon", year: 2009, posterUrl: null })} />,
    );
    expect(container.querySelector("img")).toBeNull();
    // The placeholder tile carries the title + year, centered, instead of a blank grey box (fix 4).
    const fallback = screen.getByTestId("poster-fallback");
    expect(fallback).toHaveTextContent("Moon");
    expect(fallback).toHaveTextContent("2009");
  });

  it("shows the placeholder title without a year when the year is unknown", () => {
    renderCard(<PosterCard item={recItem({ title: "Untitled", year: null, posterUrl: null })} />);
    const fallback = screen.getByTestId("poster-fallback");
    expect(fallback).toHaveTextContent("Untitled");
  });

  describe("search fit gauge (round 8: confidence now arrives on search results)", () => {
    it("renders a role=meter gauge for a search item that carries a confidence", () => {
      // hideNullFit is how Search opts in: a real confidence still earns the gauge.
      renderCard(<PosterCard item={recItem({ confidence: 0.8 })} hideNullFit={true} />);
      const gauge = screen.getByTestId("fit");
      expect(gauge).toHaveAttribute("role", "meter");
      expect(gauge).toHaveAttribute("aria-label", "Strong fit");
    });

    it("renders no gauge for a search item with null confidence (older backend / no taste)", () => {
      // Against an older backend the confidence is null; the gauge stays absent rather than showing
      // a misleading neutral sliver.
      renderCard(<PosterCard item={recItem({ confidence: null })} hideNullFit={true} />);
      expect(screen.queryByTestId("fit")).toBeNull();
    });

    it("names the open button without a fit when a null-confidence search card hides it", () => {
      renderCard(
        <PosterCard item={recItem({ title: "Moon", confidence: null })} hideNullFit={true} />,
      );
      const label = screen.getByTestId("rec-card-open").getAttribute("aria-label") ?? "";
      expect(label).toContain("Moon");
      expect(label).not.toContain("fit");
    });
  });

  describe("compact rating (search cards)", () => {
    it("shows a compact star rating when opted in and the backend sends votes", () => {
      renderCard(
        <PosterCard item={recItem({ voteAverage: 8.4, voteCount: 37000 })} showRating={true} />,
      );
      const rating = screen.getByTestId("card-rating");
      // English locale (jsdom default): one-decimal score, compact thousands.
      expect(rating).toHaveTextContent("★ 8.4 · 37K");
    });

    it("formats the count with the active locale's compact notation", () => {
      // 1,234,567 votes must compact ("1M"), not render as a full grouped number.
      renderCard(
        <PosterCard item={recItem({ voteAverage: 7.1, voteCount: 1234567 })} showRating={true} />,
      );
      expect(screen.getByTestId("card-rating")).toHaveTextContent("★ 7.1 · 1M");
    });

    it("shows the score alone when the vote count is unknown", () => {
      renderCard(<PosterCard item={recItem({ voteAverage: 6.0 })} showRating={true} />);
      expect(screen.getByTestId("card-rating")).toHaveTextContent("★ 6.0");
    });

    it("hides the rating line entirely when voteAverage is null", () => {
      renderCard(
        <PosterCard item={recItem({ voteAverage: null, voteCount: 12 })} showRating={true} />,
      );
      expect(screen.queryByTestId("card-rating")).toBeNull();
    });

    it("never shows a rating on home-row cards (default off, even with values)", () => {
      renderCard(<PosterCard item={recItem({ voteAverage: 8.4, voteCount: 37000 })} />);
      expect(screen.queryByTestId("card-rating")).toBeNull();
    });
  });

  it("no longer renders the request action on the card (round 4)", () => {
    // The request/availability control moved into the detail sheet. Even under a configured
    // availability provider (as Search wraps its grid), the *card* body must not surface it —
    // a card's only action is to open. It only appears once the sheet is opened.
    renderCard(
      <AvailabilityProvider
        value={{
          configured: true,
          results: { t1: "requestable" },
          requestingId: null,
          onRequest: () => {},
        }}
      >
        <PosterCard item={recItem({ titleId: "t1" })} />
      </AvailabilityProvider>,
    );
    expect(screen.queryByTestId("title-request")).toBeNull();
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

  it("shows the fit gauge on the popular row (R6b: popular now carries an honest taste fit)", () => {
    // The popular row's confidence is now a real taste fit (similarity to the profile centroid +
    // affinity + the standard blend), not popularity magnitude — so it earns the same gauge as the
    // taste-driven rows. Popularity only orders the row now; the gauge reads how well it fits you.
    renderCard(<RecRow row={{ key: "popular", title: "Popular", items: [recItem({})] }} />);
    expect(screen.getByTestId("fit")).toBeInTheDocument();
  });

  it("still hides the fit gauge on the continue-watching row", () => {
    // Recency warmth ("how far in") is not a taste fit, so continue-watching keeps no gauge.
    renderCard(
      <RecRow
        row={{ key: "continue_watching", title: "Continue watching", items: [recItem({})] }}
      />,
    );
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

  it("no longer renders a not-interested control on the card (round 3)", () => {
    // The ✕ moved into the detail sheet — a card's only action is to open. Guards against it
    // sneaking back onto the card's primary click target.
    renderCard(<RecRow row={{ key: "you_might_like", title: "row", items: [recItem({})] }} />);
    expect(screen.queryByTestId("not-interested")).toBeNull();
  });
});
