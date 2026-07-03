import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import i18n from "../i18n";
import { TabBar } from "../nav/TabBar";
import { LanguageSelector } from "./LanguageSelector";

describe("LanguageSelector", () => {
  afterEach(async () => {
    // Restore the shared i18n singleton so language state doesn't leak across tests.
    await i18n.changeLanguage("en");
  });

  it("switches the UI language and updates translated labels", async () => {
    await i18n.changeLanguage("en");
    render(
      <MemoryRouter>
        <LanguageSelector />
        <TabBar />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("tab-browse")).toHaveTextContent("Browse");

    fireEvent.change(screen.getByTestId("language-selector"), { target: { value: "fr" } });

    await waitFor(() => expect(screen.getByTestId("tab-browse")).toHaveTextContent("Parcourir"));
    expect(i18n.resolvedLanguage).toBe("fr");
    // L1: <html lang> follows the active language for screen readers / hyphenation.
    expect(document.documentElement.lang).toBe("fr");
  });
});
