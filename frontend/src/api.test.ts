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
    vi.stubGlobal("fetch", mockFetch({ authRequired: true, authenticated: false }));
    await expect(api.me()).resolves.toEqual({ authRequired: true, authenticated: false });
  });

  it("rejects a structurally invalid response instead of passing bad data through", async () => {
    // authRequired must be a boolean — the zod boundary should throw, not hand back a junk object.
    vi.stubGlobal("fetch", mockFetch({ authRequired: "yes", authenticated: false }));
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
