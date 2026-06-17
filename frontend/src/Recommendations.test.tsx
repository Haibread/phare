import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  ChatPanel,
  ConversionBadge,
  LoginGate,
  RecRow,
  Recommendations,
  TraktConnectNotice,
} from "./App";
import type { Conversion, RecommendationItem, RecommendationRow } from "./api";

function conversion(overrides: Partial<Conversion>): Conversion {
  return {
    shown: 0,
    converted: 0,
    rate: null,
    swingShown: 0,
    swingConverted: 0,
    swingRate: null,
    topK: 10,
    withinDays: 14,
    ...overrides,
  };
}

function recItem(overrides: Partial<RecommendationItem>): RecommendationItem {
  return {
    titleId: crypto.randomUUID(),
    title: "Arrival",
    kind: "movie",
    year: 2016,
    genres: ["Science Fiction", "Drama"],
    score: 0.9,
    isSwing: false,
    confidence: 0.8,
    explanation: "A cerebral sci-fi that fits your taste.",
    components: { score: 0.9 },
    ...overrides,
  };
}

describe("ConversionBadge", () => {
  it("shows the rate when there is matured history", () => {
    render(<ConversionBadge conversion={conversion({ shown: 8, converted: 2, rate: 0.25 })} />);
    expect(screen.getByTestId("conversion")).toHaveTextContent("25% of 8 shown");
  });

  it("falls back to a not-enough-history message", () => {
    render(<ConversionBadge conversion={conversion({ rate: null })} />);
    expect(screen.getByTestId("conversion")).toHaveTextContent("not enough history yet");
  });
});

describe("Recommendations", () => {
  it("shows an empty state when there are no rows", () => {
    render(<Recommendations rows={[]} conversion={null} busy={false} onRefresh={() => {}} />);
    expect(screen.getByTestId("recs-empty")).toBeInTheDocument();
  });

  it("renders a card per item with explanation and confidence", () => {
    const row: RecommendationRow = {
      key: "you_might_like",
      title: "You might like",
      items: [recItem({ title: "Arrival" })],
    };
    render(<RecRow row={row} />);
    expect(screen.getByText("Arrival")).toBeInTheDocument();
    expect(screen.getByText(/cerebral sci-fi/)).toBeInTheDocument();
    expect(screen.getByText(/confidence 80%/)).toBeInTheDocument();
  });

  it("badges swing picks", () => {
    const row: RecommendationRow = {
      key: "you_might_like",
      title: "You might like",
      items: [recItem({ title: "Risky Pick", isSwing: true })],
    };
    render(<RecRow row={row} />);
    expect(screen.getByTestId("swing-badge")).toBeInTheDocument();
  });
});

describe("ChatPanel", () => {
  it("renders the conversation with the agent's suggestions", () => {
    const log = [
      { role: "user" as const, text: "something funny" },
      {
        role: "agent" as const,
        text: "Here are a few comedy picks.",
        items: [recItem({ title: "Superbad" })],
      },
    ];
    render(<ChatPanel log={log} busy={false} onSend={() => {}} />);
    expect(screen.getByTestId("chat-user")).toHaveTextContent("something funny");
    expect(screen.getByTestId("chat-agent")).toHaveTextContent("comedy picks");
    expect(screen.getByText("Superbad")).toBeInTheDocument();
  });

  it("sends the trimmed message and clears the input", () => {
    const onSend = vi.fn();
    render(<ChatPanel log={[]} busy={false} onSend={onSend} />);
    const input = screen.getByTestId("chat-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "  funny and short  " } });
    fireEvent.click(screen.getByTestId("chat-send"));
    expect(onSend).toHaveBeenCalledWith("funny and short");
    expect(input.value).toBe("");
  });

  it("disables send for an empty message", () => {
    render(<ChatPanel log={[]} busy={false} onSend={() => {}} />);
    expect(screen.getByTestId("chat-send")).toBeDisabled();
  });
});

describe("TraktConnectNotice", () => {
  it("shows the user code and verification link", () => {
    render(<TraktConnectNotice userCode="ABCD-1234" verificationUrl="https://trakt.tv/activate" />);
    const notice = screen.getByTestId("trakt-connect-notice");
    expect(notice).toHaveTextContent("ABCD-1234");
    expect(screen.getByRole("link")).toHaveAttribute("href", "https://trakt.tv/activate");
  });
});

describe("LoginGate", () => {
  it("submits the password and surfaces an error", () => {
    const onLogin = vi.fn();
    render(<LoginGate onLogin={onLogin} error="Invalid password" />);
    fireEvent.change(screen.getByTestId("login-password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByTestId("login-submit"));
    expect(onLogin).toHaveBeenCalledWith("hunter2");
    expect(screen.getByTestId("login-error")).toHaveTextContent("Invalid password");
  });
});
