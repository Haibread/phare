import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourcePicker } from "./SourcePicker";

vi.mock("../api", () => ({
  api: {
    traktConnectStart: vi.fn(),
    traktConnectPoll: vi.fn(),
    syncTrakt: vi.fn(),
    syncPlex: vi.fn(),
    history: vi.fn(),
  },
}));

import { api } from "../api";

const mocked = vi.mocked(api);

afterEach(() => vi.clearAllMocks());

function tree(open: boolean) {
  const qc = new QueryClient();
  return (
    <QueryClientProvider client={qc}>
      <SourcePicker profileId="p1" open={open} onOpenChange={() => {}} onConnected={() => {}} />
    </QueryClientProvider>
  );
}

describe("SourcePicker Trakt polling", () => {
  it("stops polling once the sheet closes (no background loop, no stacking)", async () => {
    mocked.traktConnectStart.mockResolvedValue({
      deviceCode: "d",
      userCode: "U",
      verificationUrl: "http://verify",
      interval: 0.02,
      expiresIn: 100,
    });
    mocked.traktConnectPoll.mockResolvedValue({ status: "pending" });

    const { rerender } = render(tree(true));
    fireEvent.click(screen.getByTestId("source-trakt"));

    await waitFor(() => expect(mocked.traktConnectPoll).toHaveBeenCalled());

    // Close the sheet — the polling loop must abort.
    rerender(tree(false));
    const atClose = mocked.traktConnectPoll.mock.calls.length;

    // Give it well over a poll interval; without the abort this would fire several more times.
    await new Promise((r) => setTimeout(r, 100));
    expect(mocked.traktConnectPoll.mock.calls.length).toBeLessThanOrEqual(atClose + 1);
  });
});

describe("SourcePicker import progress", () => {
  it("shows the syncing view with a live count while a sync runs", async () => {
    // Hold the sync open so the syncing view stays mounted while we assert on it.
    let resolveSync: () => void = () => {};
    mocked.syncPlex.mockReturnValue(
      new Promise<never>((resolve) => {
        resolveSync = resolve as unknown as () => void;
      }),
    );
    // history.total grows across polls, mimicking the backend's incremental commits.
    let total = 0;
    mocked.history.mockImplementation(() => {
      total += 7;
      return Promise.resolve({ items: [], page: 1, perPage: 100, total });
    });

    render(tree(true));
    fireEvent.click(screen.getByTestId("source-plex"));
    fireEvent.change(screen.getByPlaceholderText("Server URL (https://…)"), {
      target: { value: "http://plex" },
    });
    fireEvent.change(screen.getByPlaceholderText("Plex token"), { target: { value: "tok" } });
    fireEvent.click(screen.getByText("Connect Plex"));

    // The syncing view appears immediately, replacing the source list.
    await waitFor(() => expect(screen.getByTestId("sync-progress")).toBeInTheDocument());
    expect(screen.queryByTestId("source-trakt")).not.toBeInTheDocument();

    // The count reflects the polled history total once the 2s interval fires.
    await waitFor(
      () => expect(screen.getByTestId("sync-progress-count").textContent).not.toBe("0"),
      { timeout: 3000 },
    );

    resolveSync();
  });
});
