import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourcePicker } from "./SourcePicker";

vi.mock("../api", () => ({
  api: {
    traktConnectStart: vi.fn(),
    traktConnectPoll: vi.fn(),
    syncTrakt: vi.fn(),
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
