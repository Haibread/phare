import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
// Initialise i18next once for the whole suite so components using useTranslation render real
// strings. jsdom's navigator.language resolves to English, so assertions on English text hold.
import "../i18n";

afterEach(() => {
  cleanup();
});
