import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ColdStart } from "./ColdStart";

vi.mock("../api", () => ({
  api: {
    seedCatalog: vi.fn(),
    loadSampleData: vi.fn(),
    onboardingStatus: vi.fn(),
    syncPlex: vi.fn(),
    history: vi.fn(),
    searchCatalog: vi.fn().mockResolvedValue({ results: [] }),
    sendTitleFeedback: vi.fn(),
    generateTaste: vi.fn(),
    sourceCapabilities: vi.fn().mockResolvedValue({
      trakt: true,
      plex: true,
      jellyfin: true,
      seerr: true,
      sampleData: true,
    }),
  },
}));

import { api } from "../api";

const mocked = vi.mocked(api);

afterEach(() => vi.clearAllMocks());

function tree() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <ColdStart profileId="p1" />
    </QueryClientProvider>
  );
}

function status(over: Partial<Record<string, boolean>> = {}) {
  return {
    catalogReady: false,
    historyReady: false,
    tasteReady: false,
    readyToBrowse: false,
    ...over,
  };
}

describe("ColdStart onboarding steps", () => {
  it("reveals the ordered steps once the sample seed starts", async () => {
    mocked.seedCatalog.mockResolvedValue({ created: 1 });
    // Hold the sample seed open so the steps view stays mounted while we assert.
    mocked.loadSampleData.mockReturnValue(new Promise<never>(() => {}));
    mocked.onboardingStatus.mockResolvedValue(status());

    render(tree());
    fireEvent.click(await screen.findByTestId("explore-sample"));

    const list = await screen.findByTestId("onboarding-steps");
    const labels = within(list)
      .getAllByRole("listitem")
      .map((li) => li.textContent);
    // Catalog → history → taste, in that order.
    expect(labels).toEqual([
      expect.stringContaining("Building the sample catalog"),
      expect.stringContaining("Loading a demo watch history"),
      expect.stringContaining("Analyzing your taste"),
    ]);
  });

  it("marks a step done as the polled status reports it ready", async () => {
    mocked.seedCatalog.mockResolvedValue({ created: 1 });
    mocked.loadSampleData.mockReturnValue(new Promise<never>(() => {}));
    // Catalog is ready; history/taste are still pending.
    mocked.onboardingStatus.mockResolvedValue(status({ catalogReady: true }));

    render(tree());
    fireEvent.click(await screen.findByTestId("explore-sample"));

    const list = await screen.findByTestId("onboarding-steps");
    await waitFor(() => {
      const items = within(list).getAllByRole("listitem");
      expect(items[0]?.dataset.state).toBe("done");
      expect(items[1]?.dataset.state).toBe("active"); // the first not-yet-done step
      expect(items[2]?.dataset.state).toBe("pending");
    });
  });
});

describe("ColdStart sample-data escape hatch", () => {
  it("hides the sample button when the server disables it (production)", async () => {
    mocked.sourceCapabilities.mockResolvedValueOnce({
      trakt: true,
      plex: true,
      jellyfin: true,
      seerr: true,
      sampleData: false,
    });

    render(tree());
    // The connect CTA is always there; give the capabilities query a tick to resolve.
    await screen.findByTestId("open-source-picker");
    expect(screen.queryByTestId("explore-sample")).not.toBeInTheDocument();
  });

  it("shows an error state instead of hanging when the catalog seed 403s", async () => {
    // In prod the catalog seed 403s; the rejection must surface the ErrorState, not leave the user
    // stuck on the fake-progress screen forever (the guard used to ignore catalog.isError).
    mocked.seedCatalog.mockRejectedValue(new Error("Sample catalog is disabled in production"));
    mocked.loadSampleData.mockReturnValue(new Promise<never>(() => {}));
    mocked.onboardingStatus.mockResolvedValue(status({ catalogReady: true }));

    render(tree());
    fireEvent.click(await screen.findByTestId("explore-sample"));

    // The onboarding steps must NOT take over; the error is shown instead.
    await waitFor(() => {
      expect(screen.getByTestId("error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("error").textContent).toContain("disabled in production");
    expect(screen.queryByTestId("onboarding-steps")).not.toBeInTheDocument();
  });
});

describe("ColdStart start-from-scratch (r7-1)", () => {
  it("offers the escape even when sample data is disabled, and opens the seeding step", async () => {
    // Production-like: no sample data. The scratch path must still be available — it's the only one
    // that works without a connected library.
    mocked.sourceCapabilities.mockResolvedValueOnce({
      trakt: true,
      plex: true,
      jellyfin: true,
      seerr: true,
      sampleData: false,
    });

    render(tree());
    await screen.findByTestId("open-source-picker");
    expect(screen.queryByTestId("explore-sample")).not.toBeInTheDocument();

    // The scratch button is present and reveals the by-hand seeding step.
    fireEvent.click(screen.getByTestId("start-from-scratch"));
    expect(await screen.findByTestId("scratch-start")).toBeInTheDocument();
    expect(screen.getByTestId("scratch-search")).toBeInTheDocument();
  });
});

describe("ColdStart end-of-sync confirmation (D4)", () => {
  it("shows an imported-count confirmation before landing, instead of hard-cutting", async () => {
    mocked.syncPlex.mockResolvedValue({
      created: 42,
      updated: 0,
      skipped: 0,
      titlesCreated: 42,
      tasteBuilding: false,
    });
    mocked.history.mockResolvedValue({ items: [], page: 1, perPage: 100, total: 42 });

    render(tree());
    // Open the source picker and drive a Plex sync to completion.
    fireEvent.click(screen.getByTestId("open-source-picker"));
    fireEvent.click(await screen.findByTestId("source-plex"));
    fireEvent.change(screen.getByPlaceholderText("Server URL (https://…)"), {
      target: { value: "http://plex" },
    });
    fireEvent.change(screen.getByPlaceholderText("Plex token"), { target: { value: "tok" } });
    fireEvent.click(screen.getByText("Connect Plex"));

    // The confirmation appears with the imported count — not an immediate switch to Browse.
    const confirmation = await screen.findByTestId("sync-confirmation");
    expect(confirmation.textContent).toContain("42");
    expect(screen.getByTestId("sync-continue")).toBeInTheDocument();
  });
});
