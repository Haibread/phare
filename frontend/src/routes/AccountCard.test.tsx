import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AccountCard } from "./AccountCard";

vi.mock("../api", () => ({
  api: { logout: vi.fn(), changePassword: vi.fn() },
  setAuthToken: vi.fn(),
}));

import { api, setAuthToken } from "../api";

const mocked = vi.mocked(api);
afterEach(() => vi.clearAllMocks());

function render_() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AccountCard />
    </QueryClientProvider>,
  );
}

describe("AccountCard (I5)", () => {
  it("signs out: calls logout and drops the local token", async () => {
    mocked.logout.mockResolvedValue(null);
    render_();
    fireEvent.click(screen.getByTestId("logout"));
    await waitFor(() => expect(mocked.logout).toHaveBeenCalled());
    expect(setAuthToken).toHaveBeenCalledWith(null);
  });

  it("changes the password and swaps in the fresh token", async () => {
    mocked.changePassword.mockResolvedValue({ token: "fresh-token" });
    render_();
    fireEvent.change(screen.getByTestId("current-password"), { target: { value: "oldpassword" } });
    fireEvent.change(screen.getByTestId("new-password"), { target: { value: "newpassword1" } });
    fireEvent.click(screen.getByTestId("change-password"));
    await waitFor(() =>
      expect(mocked.changePassword).toHaveBeenCalledWith("oldpassword", "newpassword1"),
    );
    expect(setAuthToken).toHaveBeenCalledWith("fresh-token");
    expect(await screen.findByTestId("account-notice")).toBeInTheDocument();
  });

  it("labels the password fields for password managers", () => {
    render_();
    expect(screen.getByLabelText("Current password")).toHaveAttribute("name", "current-password");
    expect(screen.getByLabelText("New password (10+ characters)")).toHaveAttribute(
      "autocomplete",
      "new-password",
    );
  });
});
