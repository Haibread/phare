import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourcePicker } from "./SourcePicker";

// Keep the real ApiError + syncPartialFailureCount (the component reads structured error data);
// only the `api` client's methods are stubbed.
vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      traktConnectStart: vi.fn(),
      traktConnectPoll: vi.fn(),
      syncTrakt: vi.fn(),
      syncPlex: vi.fn(),
      syncJellyfin: vi.fn(),
      jellyfinUsers: vi.fn(),
      history: vi.fn(),
      sourceCapabilities: vi.fn().mockResolvedValue({
        trakt: true,
        plex: true,
        jellyfin: true,
        seerr: true,
        sampleData: true,
      }),
    },
  };
});

import { ApiError, api } from "../api";

const mocked = vi.mocked(api);

afterEach(() => vi.clearAllMocks());

function tree(open: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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

describe("SourcePicker Trakt cancel + non-blocking wait (r7-2)", () => {
  it("keeps other sources enabled while the Trakt poll waits, and cancels on demand", async () => {
    mocked.traktConnectStart.mockResolvedValue({
      deviceCode: "d",
      userCode: "U",
      verificationUrl: "http://verify",
      interval: 100, // long, so we stay in the waiting phase for the assertions
      expiresIn: 1000,
    });
    mocked.traktConnectPoll.mockResolvedValue({ status: "pending" });

    render(tree(true));
    fireEvent.click(screen.getByTestId("source-trakt"));

    // The device-code notice shows and the OTHER sources stay usable (the mistap is escapable).
    await waitFor(() => expect(screen.getByTestId("trakt-connect-notice")).toBeInTheDocument());
    expect(screen.getByTestId("source-plex")).not.toBeDisabled();
    expect(screen.getByTestId("source-jellyfin")).not.toBeDisabled();
    // The Trakt button itself re-arms (tapping again would just restart the same flow).
    expect(screen.getByTestId("source-trakt")).toBeDisabled();

    // The explicit cancel affordance aborts the poll and clears the notice.
    fireEvent.click(screen.getByTestId("trakt-cancel"));
    await waitFor(() =>
      expect(screen.queryByTestId("trakt-connect-notice")).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("source-trakt")).not.toBeDisabled();
  });

  it("aborts the Trakt poll when the user picks another source instead", async () => {
    mocked.traktConnectStart.mockResolvedValue({
      deviceCode: "d",
      userCode: "U",
      verificationUrl: "http://verify",
      interval: 100,
      expiresIn: 1000,
    });
    mocked.traktConnectPoll.mockResolvedValue({ status: "pending" });

    render(tree(true));
    fireEvent.click(screen.getByTestId("source-trakt"));
    await waitFor(() => expect(screen.getByTestId("trakt-connect-notice")).toBeInTheDocument());

    // Selecting Plex supersedes the Trakt poll: its notice clears and the Plex form opens.
    fireEvent.click(screen.getByTestId("source-plex"));
    await waitFor(() =>
      expect(screen.queryByTestId("trakt-connect-notice")).not.toBeInTheDocument(),
    );
    expect(screen.getByPlaceholderText("Plex token")).toBeInTheDocument();
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

  it("flags a stalled progress counter after repeated poll failures, and retries (D3)", async () => {
    vi.useFakeTimers();
    try {
      mocked.syncPlex.mockReturnValue(new Promise<never>(() => {})); // never resolves
      mocked.history.mockRejectedValue(new Error("network"));

      render(tree(true));
      fireEvent.click(screen.getByTestId("source-plex"));
      fireEvent.change(screen.getByPlaceholderText("Server URL (https://…)"), {
        target: { value: "http://plex" },
      });
      fireEvent.change(screen.getByPlaceholderText("Plex token"), { target: { value: "tok" } });
      fireEvent.click(screen.getByText("Connect Plex"));

      // Three failed 2s polls cross the stall threshold.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6100);
      });
      expect(screen.getByTestId("sync-stalled")).toBeInTheDocument();

      // Retry with a now-healthy endpoint clears the stall and resumes the count.
      mocked.history.mockResolvedValue({ items: [], page: 1, perPage: 100, total: 5 });
      await act(async () => {
        fireEvent.click(screen.getByText("Retry"));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.queryByTestId("sync-stalled")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("SourcePicker partial-sync failure (G3)", () => {
  it("shows how much already imported and resumes the import on retry", async () => {
    // First sync dies mid-stream after 42 titles; the retry re-runs and succeeds.
    const partial = new ApiError(502, "Bad Gateway", {
      data: { code: "sync_partial_failure", ingested: 42 },
    });
    mocked.syncPlex.mockRejectedValueOnce(partial).mockResolvedValue({
      created: 42,
      updated: 0,
      skipped: 0,
      titlesCreated: 42,
      tasteBuilding: false,
    });
    mocked.history.mockResolvedValue({ items: [], page: 1, perPage: 100, total: 42 });

    render(tree(true));
    fireEvent.click(screen.getByTestId("source-plex"));
    fireEvent.change(screen.getByPlaceholderText("Server URL (https://…)"), {
      target: { value: "http://plex" },
    });
    fireEvent.change(screen.getByPlaceholderText("Plex token"), { target: { value: "tok" } });
    fireEvent.click(screen.getByText("Connect Plex"));

    // The honest partial-failure notice shows the imported count, not a bare error.
    const notice = await screen.findByTestId("sync-partial");
    expect(within(notice).getByTestId("sync-partial-count").textContent).toBe("42");

    // Retry re-runs the same import; on success the partial notice clears.
    fireEvent.click(screen.getByTestId("sync-partial-retry"));
    await waitFor(() => expect(mocked.syncPlex).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByTestId("sync-partial")).not.toBeInTheDocument());
  });
});

describe("SourcePicker capabilities (D1)", () => {
  it("disables a source the server hasn't configured, with a soft note", async () => {
    mocked.sourceCapabilities.mockResolvedValue({
      trakt: false,
      plex: true,
      jellyfin: true,
      seerr: true,
      sampleData: true,
    });

    render(tree(true));

    await waitFor(() => expect(screen.getByTestId("source-trakt")).toBeDisabled());
    expect(screen.getByTestId("source-trakt-unconfigured")).toBeInTheDocument();
    // A configured source stays enabled.
    expect(screen.getByTestId("source-plex")).not.toBeDisabled();
  });
});

describe("SourcePicker Jellyfin user picker (D2)", () => {
  it("lists users and offers a select instead of a raw GUID field", async () => {
    mocked.jellyfinUsers.mockResolvedValue([
      { id: "u1", name: "Alice" },
      { id: "u2", name: "Bob" },
    ]);

    render(tree(true));
    fireEvent.click(screen.getByTestId("source-jellyfin"));
    fireEvent.change(screen.getByPlaceholderText("Server URL (https://…)"), {
      target: { value: "http://jf" },
    });
    fireEvent.change(screen.getByPlaceholderText("API key"), { target: { value: "key" } });
    fireEvent.click(screen.getByTestId("jellyfin-list-users"));

    const select = await screen.findByTestId("jellyfin-user-select");
    expect(within(select).getByText("Alice")).toBeInTheDocument();
    expect(within(select).getByText("Bob")).toBeInTheDocument();
  });
});
