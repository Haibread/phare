import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProfileProvider } from "../app/ProfileContext";
import { Chat } from "./Chat";

vi.mock("../api", () => ({
  api: {
    chat: vi.fn(),
    chatOpening: vi.fn(),
    undoChatAction: vi.fn(),
  },
}));

import { api } from "../api";

const mocked = vi.mocked(api);

afterEach(() => vi.clearAllMocks());

function renderChat() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ProfileProvider value="p1">
        <Chat />
      </ProfileProvider>
    </QueryClientProvider>,
  );
}

describe("Chat write actions", () => {
  it("shows an undoable action chip after a write and reverses it on undo", async () => {
    mocked.chatOpening.mockResolvedValue({ greeting: null });
    mocked.chat.mockResolvedValue({
      replyText: "Got it — logged Dune as loved.",
      intent: { maxRuntime: null, includeGenres: [], excludeGenres: [], mood: null },
      items: [],
      actions: [{ kind: "logged_signal", summary: "logged Dune as loved", undoToken: "event:abc" }],
    });
    mocked.undoChatAction.mockResolvedValue({ undone: true });

    renderChat();
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "I saw Dune and loved it" },
    });
    fireEvent.click(screen.getByTestId("chat-send"));

    const undoBtn = await screen.findByTestId("chat-undo");
    expect(screen.getByTestId("chat-action")).toHaveTextContent("logged Dune as loved");

    fireEvent.click(undoBtn);
    await waitFor(() => expect(mocked.undoChatAction).toHaveBeenCalledWith("p1", "event:abc"));
    await screen.findByText(/undone/);
  });

  it("uses the proactive opening greeting when there are pending plans", async () => {
    mocked.chatOpening.mockResolvedValue({ greeting: "Did you watch Heat? How was it?" });

    renderChat();
    expect(await screen.findByText(/Did you watch Heat/)).toBeInTheDocument();
  });
});
