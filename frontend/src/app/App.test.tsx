import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";

vi.mock("../api", () => ({
  api: {
    me: vi.fn(),
    listProfiles: vi.fn(),
    history: vi.fn(),
    createProfile: vi.fn(),
    login: vi.fn(),
  },
  setAuthToken: vi.fn(),
}));

import { api } from "../api";

const mocked = vi.mocked(api);
const profilePage = {
  items: [{ id: "p1", displayName: "Me", createdAt: "x" }],
  page: 1,
  perPage: 20,
  total: 1,
};

function renderApp() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("App onboarding gate", () => {
  it("shows the error state (not onboarding) when history fails — an existing user must not be bounced to cold-start", async () => {
    mocked.me.mockResolvedValue({ authRequired: false, authenticated: false });
    mocked.listProfiles.mockResolvedValue(profilePage);
    mocked.history.mockRejectedValue(new Error("backend down"));

    renderApp();

    expect(await screen.findByTestId("error")).toBeInTheDocument();
    expect(screen.queryByTestId("cold-start")).not.toBeInTheDocument();
  });

  it("shows cold-start onboarding only when history is genuinely empty", async () => {
    mocked.me.mockResolvedValue({ authRequired: false, authenticated: false });
    mocked.listProfiles.mockResolvedValue(profilePage);
    mocked.history.mockResolvedValue({ items: [], page: 1, perPage: 100, total: 0 });

    renderApp();

    expect(await screen.findByTestId("cold-start")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("error")).not.toBeInTheDocument());
  });
});
