import { QueryClient } from "@tanstack/react-query";
import i18n from "i18next";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Recommendations are mildly expensive; don't refetch on every focus.
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

// Language-dependent responses (recommendations, dynamic rows, taste, title detail, search) are
// fetched with an Accept-Language header, so their cached payloads belong to the language they were
// fetched in. The query *keys* don't encode the language (it's an ambient request header, not a key
// input), so switching the UI language would otherwise keep serving stale English rows/blurbs until
// a manual reload. Rather than thread the active language into every query key across the app,
// invalidate the whole cache on a language change: every active query refetches immediately (so
// what's on screen swaps to the new language) and every inactive one is marked stale (so a later
// visit refetches too). Blanket-invalidate over per-key rekeying keeps the fix in one place and
// can't be forgotten when a new query is added.
i18n.on("languageChanged", () => {
  void queryClient.invalidateQueries();
});
