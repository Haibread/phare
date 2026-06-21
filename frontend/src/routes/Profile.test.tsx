import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProfileProvider } from "../app/ProfileContext";
import { Profile } from "./Profile";

vi.mock("../api", () => ({
  api: {
    // taste is 404 (no profile yet) so the empty state with the Generate button renders.
    getTaste: vi.fn().mockRejectedValue(new Error("not found")),
    history: vi.fn().mockResolvedValue({ items: [] }),
    conversion: vi.fn().mockResolvedValue({ rate: null, shown: 0, topK: 10, withinDays: 30 }),
    listSources: vi.fn().mockResolvedValue([]),
    listCommitments: vi.fn().mockResolvedValue({ items: [] }),
    listMemory: vi.fn().mockResolvedValue({ items: [] }),
    generateTaste: vi.fn(),
  },
}));

import { api } from "../api";

const mocked = vi.mocked(api);

afterEach(() => {
  vi.clearAllMocks();
});

function renderProfile() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ProfileProvider value="p1">
        <Profile />
      </ProfileProvider>
    </QueryClientProvider>,
  );
}

describe("Profile taste generation", () => {
  it("calls generateTaste and shows the pending label when the button is clicked", async () => {
    // Keep the mutation pending so we can assert the generating state.
    let resolveGenerate: () => void = () => {};
    mocked.generateTaste.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveGenerate = () => resolve({} as never);
        }),
    );

    renderProfile();

    const button = await screen.findByTestId("taste-generate");
    fireEvent.click(button);

    await waitFor(() => expect(mocked.generateTaste).toHaveBeenCalledWith("p1"));
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Generating…");

    resolveGenerate();
  });

  it("surfaces the backend error when generation fails", async () => {
    mocked.generateTaste.mockRejectedValue(new Error("LLM is not configured (set LLM_API_KEY)"));

    renderProfile();

    const button = await screen.findByTestId("taste-generate");
    fireEvent.click(button);

    const error = await screen.findByTestId("taste-generate-error");
    expect(error).toHaveTextContent("LLM is not configured (set LLM_API_KEY)");
  });
});
