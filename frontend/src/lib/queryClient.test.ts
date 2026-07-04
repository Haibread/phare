import { QueryObserver } from "@tanstack/react-query";
import i18n from "i18next";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "./queryClient";

describe("queryClient language handling", () => {
  afterEach(async () => {
    queryClient.clear();
    // Restore the shared i18n singleton so language state doesn't leak across tests.
    await i18n.changeLanguage("en");
  });

  it("refetches an active query when the UI language changes", async () => {
    let calls = 0;
    const queryFn = vi.fn(async () => {
      calls += 1;
      return `payload-${calls}`;
    });

    // A mounted observer stands in for a component subscribed to a language-dependent query.
    const observer = new QueryObserver(queryClient, { queryKey: ["taste"], queryFn });
    const unsubscribe = observer.subscribe(() => {});
    await vi.waitFor(() => expect(observer.getCurrentResult().data).toBe("payload-1"));
    expect(queryFn).toHaveBeenCalledTimes(1);

    // Switching the language must not keep serving the stale (English) payload.
    await i18n.changeLanguage("fr");
    await vi.waitFor(() => expect(observer.getCurrentResult().data).toBe("payload-2"));
    expect(queryFn).toHaveBeenCalledTimes(2);

    unsubscribe();
  });
});
