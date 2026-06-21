/** i18next setup: English + French, default from the browser, choice persisted to localStorage.
 *
 * Resources are bundled per namespace (one JSON file per feature, per language) so they load
 * synchronously — no Suspense needed. The active language is mirrored onto the API client as
 * Accept-Language so the backend can localise the text it generates (TMDB metadata, LLM output). */

import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";
import { setApiLanguage } from "./api";

import enAuth from "./locales/en/auth.json";
import enBrowse from "./locales/en/browse.json";
import enChat from "./locales/en/chat.json";
import enCommon from "./locales/en/common.json";
import enNav from "./locales/en/nav.json";
import enOnboarding from "./locales/en/onboarding.json";
import enProfile from "./locales/en/profile.json";
import enSearch from "./locales/en/search.json";
import enTitle from "./locales/en/title.json";

import frAuth from "./locales/fr/auth.json";
import frBrowse from "./locales/fr/browse.json";
import frChat from "./locales/fr/chat.json";
import frCommon from "./locales/fr/common.json";
import frNav from "./locales/fr/nav.json";
import frOnboarding from "./locales/fr/onboarding.json";
import frProfile from "./locales/fr/profile.json";
import frSearch from "./locales/fr/search.json";
import frTitle from "./locales/fr/title.json";

export const SUPPORTED_LANGUAGES = ["en", "fr"] as const;
export type Language = (typeof SUPPORTED_LANGUAGES)[number];

/** localStorage key holding the chosen language (also the i18next detector cache key). */
export const LANGUAGE_STORAGE_KEY = "phare.language";

export const LANGUAGE_LABELS: Record<Language, string> = {
  en: "English",
  fr: "Français",
};

const resources = {
  en: {
    common: enCommon,
    nav: enNav,
    browse: enBrowse,
    chat: enChat,
    profile: enProfile,
    search: enSearch,
    onboarding: enOnboarding,
    auth: enAuth,
    title: enTitle,
  },
  fr: {
    common: frCommon,
    nav: frNav,
    browse: frBrowse,
    chat: frChat,
    profile: frProfile,
    search: frSearch,
    onboarding: frOnboarding,
    auth: frAuth,
    title: frTitle,
  },
} as const;

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    // Synchronous init: resources are bundled, so there's no async load — this avoids a
    // first-render flash of translation keys (and keeps tests simple: t() is ready immediately).
    initAsync: false,
    fallbackLng: "en",
    supportedLngs: SUPPORTED_LANGUAGES,
    // Treat e.g. "fr-FR" / "fr-CA" from the browser as "fr".
    nonExplicitSupportedLngs: true,
    load: "languageOnly",
    defaultNS: "common",
    interpolation: { escapeValue: false },
    detection: {
      order: ["localStorage", "navigator"],
      caches: ["localStorage"],
      lookupLocalStorage: LANGUAGE_STORAGE_KEY,
    },
  });

// Keep the API client's Accept-Language in lockstep with the active UI language.
setApiLanguage(i18n.resolvedLanguage ?? "en");
i18n.on("languageChanged", (lng) => setApiLanguage(lng));

export default i18n;
