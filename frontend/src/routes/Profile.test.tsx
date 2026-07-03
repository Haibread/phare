import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProfileProvider } from "../app/ProfileContext";
import { Profile } from "./Profile";

vi.mock("../api", async (importActual) => {
  // Keep the real ApiError + isLLMUnavailable so the 503 llm_unavailable path is exercised for real
  // (the component keys the localized message off isLLMUnavailable), while the network calls are stubbed.
  const actual = await importActual<typeof import("../api")>();
  return {
    ApiError: actual.ApiError,
    isLLMUnavailable: actual.isLLMUnavailable,
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
  };
});

import { ApiError, api } from "../api";

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

  it("shows a localized 'try again' message when the LLM is unavailable (503)", async () => {
    // F3/M9.2: a manual regenerate that hits an unreachable provider or a spent budget returns a 503
    // with { code: "llm_unavailable" }. The UI must show the friendly localized message, not the raw
    // "Service Unavailable" status text.
    mocked.generateTaste.mockRejectedValue(
      new ApiError(503, "Service Unavailable", {
        data: { code: "llm_unavailable", reason: "llm_unreachable" },
      }),
    );

    renderProfile();

    const button = await screen.findByTestId("taste-generate");
    fireEvent.click(button);

    const error = await screen.findByTestId("taste-generate-error");
    expect(error).toHaveTextContent("Couldn't reach the AI to regenerate your taste right now.");
    expect(error).not.toHaveTextContent("Service Unavailable");
  });

  it("offers Regenerate even when a taste profile already exists", async () => {
    // The bug: the button only showed in the empty state, so a user with taste had no way to
    // recompute it. It must always be available — labelled "Regenerate" once taste exists.
    mocked.getTaste.mockResolvedValueOnce({
      summary: "Loves cerebral sci-fi and tense thrillers",
      structured: { likes: ["Science Fiction"], hard_avoids: [] },
      confidence: 0.7,
      userOverrides: {},
    } as never);

    renderProfile();

    expect(await screen.findByTestId("taste-summary")).toBeInTheDocument();
    const button = await screen.findByTestId("taste-generate");
    expect(button).toHaveTextContent("Regenerate");
  });
});
