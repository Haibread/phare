import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatStreamHandlers, ChatStreamOptions } from "../api";
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
      async (
        _profileId: string,
        _message: string,
        _options: ChatStreamOptions,
        handlers: ChatStreamHandlers,
      ) => {
        handlers.onMeta?.({
          degraded: false,
          intent: { maxRuntime: null, includeGenres: [], excludeGenres: [], mood: null },
          items: [],
          actions: [
            { kind: "logged_signal", summary: "logged Dune as loved", undoToken: "event:abc" },
          ],
          suggestions: [],
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
      async (
        _profileId: string,
        _message: string,
        _options: ChatStreamOptions,
        handlers: ChatStreamHandlers,
      ) => {
        handlers.onMeta?.({
          degraded: false,
          intent: { maxRuntime: null, includeGenres: [], excludeGenres: [], mood: null },
          items: [],
          actions: [],
          suggestions: [],
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
      async (
        _profileId: string,
        _message: string,
        _options: ChatStreamOptions,
        handlers: ChatStreamHandlers,
      ) => {
        handlers.onMeta?.({
          degraded: true,
          intent: { maxRuntime: null, includeGenres: [], excludeGenres: [], mood: null },
          items: [],
          actions: [],
          suggestions: [],
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
      async (
        _profileId: string,
        _message: string,
        _options: ChatStreamOptions,
        handlers: ChatStreamHandlers,
      ) => {
        handlers.onMeta?.({
          degraded: false,
          intent: { maxRuntime: null, includeGenres: [], excludeGenres: [], mood: null },
          items: [],
          actions: [],
          suggestions: [],
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

  it("renders clarify suggestions and sends the tapped one as the next turn", async () => {
    mocked.chatOpening.mockResolvedValue({ greeting: null });
    mocked.chatStream.mockImplementation(
      async (
        _profileId: string,
        _message: string,
        _options: ChatStreamOptions,
        handlers: ChatStreamHandlers,
      ) => {
        handlers.onMeta?.({
          degraded: false,
          intent: { maxRuntime: null, includeGenres: [], excludeGenres: [], mood: null },
          items: [],
          actions: [],
          suggestions: ["a film", "a series", "Surprise me"],
        });
        handlers.onDelta?.("A film or a series?");
        handlers.onDone?.();
      },
    );

    renderChat();
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "recommend something" },
    });
    fireEvent.click(screen.getByTestId("chat-send"));
    await screen.findByText("A film or a series?");

    const chips = await screen.findAllByTestId("chat-clarify-suggestion");
    expect(chips.map((c) => c.textContent)).toEqual(["a film", "a series", "Surprise me"]);

    const series = chips[1];
    if (!series) throw new Error("expected a second suggestion chip");
    fireEvent.click(series); // "a series"
    await waitFor(() => expect(mocked.chatStream).toHaveBeenCalledTimes(2));
    const followUp = mocked.chatStream.mock.calls[1];
    expect(followUp?.[1]).toBe("a series"); // the tapped chip is sent as the next message
    // The clarify turn had no picks, so its throwaway intent must NOT become an active filter.
    expect(followUp?.[2].activeIntent).toBeNull();
  });

  it("uses the proactive opening greeting when there are pending plans", async () => {
    mocked.chatOpening.mockResolvedValue({ greeting: "Did you watch Heat? How was it?" });

    renderChat();
    expect(await screen.findByText(/Did you watch Heat/)).toBeInTheDocument();
  });

  it("replays the prior conversation and active filters on the next turn (not a cold start)", async () => {
    mocked.chatOpening.mockResolvedValue({ greeting: null });
    const firstIntent = {
      maxRuntime: 40,
      includeGenres: ["comedy"],
      excludeGenres: [],
      mood: null,
    };
    mocked.chatStream.mockImplementation(
      async (
        _profileId: string,
        _message: string,
        _options: ChatStreamOptions,
        handlers: ChatStreamHandlers,
      ) => {
        handlers.onMeta?.({
          degraded: false,
          intent: firstIntent,
          // A real pick — so this counts as a recommendation turn whose filters carry forward.
          items: [
            {
              titleId: "t1",
              title: "Paddington 2",
              kind: "movie",
              year: 2017,
              genres: ["comedy"],
              score: 0.9,
              isSwing: false,
              confidence: 0.8,
              explanation: null,
              posterUrl: null,
              components: {},
              watched: false,
            },
          ],
          actions: [],
          suggestions: [],
        });
        handlers.onDelta?.("Try Paddington 2.");
        handlers.onDone?.();
      },
    );

    renderChat();
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "something funny" } });
    fireEvent.click(screen.getByTestId("chat-send"));
    await screen.findByText("Try Paddington 2.");

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "even shorter" } });
    fireEvent.click(screen.getByTestId("chat-send"));

    await waitFor(() => expect(mocked.chatStream).toHaveBeenCalledTimes(2));
    const secondCall = mocked.chatStream.mock.calls[1];
    if (!secondCall) throw new Error("expected a second chatStream call");
    const { history, activeIntent } = secondCall[2];
    // The second turn carries the first exchange — so "even shorter" can resolve against it…
    expect(history).toEqual([
      { role: "user", text: "something funny" },
      { role: "agent", text: "Try Paddington 2." },
    ]);
    // …but the current message is never folded into its own history.
    expect(history).not.toContainEqual({ role: "user", text: "even shorter" });
    // …and the filters in effect (≤40 min comedy) ride along so the planner can tighten them.
    expect(activeIntent).toEqual(firstIntent);
  });
});
