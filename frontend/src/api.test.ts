import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

function mockFetch(body: unknown, { ok = true, status = 200 } = {}) {
  return vi.fn().mockResolvedValue({
    ok,
    status,
    statusText: "OK",
    json: () => Promise.resolve(body),
  });
}

describe("api request() zod boundary", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("parses a well-formed response", async () => {
    const me = { needsSetup: false, registrationOpen: false, authenticated: false, user: null };
    vi.stubGlobal("fetch", mockFetch(me));
    await expect(api.me()).resolves.toEqual(me);
  });

  it("rejects a structurally invalid response instead of passing bad data through", async () => {
    // needsSetup must be a boolean — the zod boundary should throw, not hand back a junk object.
    vi.stubGlobal(
      "fetch",
      mockFetch({ needsSetup: "yes", registrationOpen: false, authenticated: false, user: null }),
    );
    await expect(api.me()).rejects.toThrow();
  });

  it("surfaces the backend error detail on a non-2xx response", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({ detail: "instance is protected" }, { ok: false, status: 401 }),
    );
    await expect(api.me()).rejects.toThrow("instance is protected");
  });
});
