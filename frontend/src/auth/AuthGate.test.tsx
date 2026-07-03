import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AuthGate } from "./AuthGate";

const noop = () => {};

function props(overrides: Partial<React.ComponentProps<typeof AuthGate>> = {}) {
  return {
    needsSetup: false,
    registrationOpen: true,
    onLogin: noop,
    onRegister: noop,
    onPlexToken: noop,
    error: null,
    ...overrides,
  };
}

describe("AuthGate accessibility (D6)", () => {
  it("associates a label with each field and names them for password managers", () => {
    render(<AuthGate {...props()} />);

    // getByLabelText passes only when each input has an associated <label> (or aria-label).
    const email = screen.getByLabelText("Email");
    const password = screen.getByLabelText("Password");
    expect(email).toHaveAttribute("name", "email");
    expect(email).toHaveAttribute("autocomplete", "email");
    expect(password).toHaveAttribute("name", "password");
    // Login mode asks the browser for the existing password.
    expect(password).toHaveAttribute("autocomplete", "current-password");
  });

  it("labels the display-name field and requests a new password in setup mode", () => {
    render(<AuthGate {...props({ needsSetup: true })} />);

    expect(screen.getByLabelText("Display name")).toHaveAttribute("name", "name");
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "new-password");
  });
});
