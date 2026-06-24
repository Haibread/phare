import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatStreamHandlers } from "../api";
import { ChatProvider } from "../app/ChatContext";
import { ProfileProvider } from "../app/ProfileContext";
import { Chat } from "./Chat";

vi.mock("../api", () => ({
  api: {
    chatStream: vi.fn(),
    chatOpening: vi.fn(),
    undoChatAction: vi.fn(),
  },
}));

import { api } from "../api";

const mocked = vi.mocked(api);

afterEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear(); // the chat log is mirrored to sessionStorage; isolate tests
});

function renderChat() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ProfileProvider value="p1">
        <ChatProvider>
          <Chat />
        </ChatProvider>
      </ProfileProvider>
    </QueryClientProvider>,
  );
}

describe("Chat write actions", () => {
  it("shows an undoable action chip after a streamed write and reverses it on undo", async () => {
    mocked.chatOpening.mockResolvedValue({ greeting: null });
    mocked.chatStream.mockImplementation(
      async (_profileId: string, _message: string, handlers: ChatStreamHandlers) => {
        handlers.onMeta?.({
          degraded: false,
          intent: { maxRuntime: null, includeGenres: [], excludeGenres: [], mood: null },
          items: [],
          actions: [
            { kind: "logged_signal", summary: "logged Dune as loved", undoToken: "event:abc" },
          ],
        });
        handlers.onDelta?.("Got it — logged Dune as loved.");
        handlers.onDone?.();
      },
    );
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

  it("streams the reply text into the agent bubble", async () => {
    mocked.chatOpening.mockResolvedValue({ greeting: null });
    mocked.chatStream.mockImplementation(
      async (_profileId: string, _message: string, handlers: ChatStreamHandlers) => {
        handlers.onMeta?.({
          degraded: false,
          intent: { maxRuntime: null, includeGenres: [], excludeGenres: [], mood: null },
          items: [],
          actions: [],
        });
        handlers.onDelta?.("A few ");
        handlers.onDelta?.("ideas for you.");
        handlers.onDone?.();
      },
    );

    renderChat();
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "something to watch" } });
    fireEvent.click(screen.getByTestId("chat-send"));

    // The two chunks assemble into one reply bubble.
    expect(await screen.findByText("A few ideas for you.")).toBeInTheDocument();
  });

  it("shows a reduced-mode note when the turn degraded", async () => {
    mocked.chatOpening.mockResolvedValue({ greeting: null });
    mocked.chatStream.mockImplementation(
      async (_profileId: string, _message: string, handlers: ChatStreamHandlers) => {
        handlers.onMeta?.({
          degraded: true,
          intent: { maxRuntime: null, includeGenres: [], excludeGenres: [], mood: null },
          items: [],
          actions: [],
        });
        handlers.onDelta?.("Here are some picks.");
        handlers.onDone?.();
      },
    );

    renderChat();
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "I loved Dune" } });
    fireEvent.click(screen.getByTestId("chat-send"));

    expect(await screen.findByTestId("chat-degraded")).toBeInTheDocument();
  });

  it("clears the conversation when New chat is clicked", async () => {
    mocked.chatOpening.mockResolvedValue({ greeting: null });
    mocked.chatStream.mockImplementation(
      async (_profileId: string, _message: string, handlers: ChatStreamHandlers) => {
        handlers.onMeta?.({
          degraded: false,
          intent: { maxRuntime: null, includeGenres: [], excludeGenres: [], mood: null },
          items: [],
          actions: [],
        });
        handlers.onDelta?.("Sure thing.");
        handlers.onDone?.();
      },
    );

    renderChat();
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } });
    fireEvent.click(screen.getByTestId("chat-send"));
    await screen.findByText("Sure thing.");

    fireEvent.click(screen.getByTestId("chat-new"));
    await waitFor(() => expect(screen.queryByText("Sure thing.")).not.toBeInTheDocument());
    expect(screen.getByTestId("chat-greeting")).toBeInTheDocument(); // back to the empty state
  });

  it("uses the proactive opening greeting when there are pending plans", async () => {
    mocked.chatOpening.mockResolvedValue({ greeting: "Did you watch Heat? How was it?" });

    renderChat();
    expect(await screen.findByText(/Did you watch Heat/)).toBeInTheDocument();
  });
});
