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

  it("tolerates a title detail from an older backend missing the round-8 metadata", async () => {
    // The richer fields (directors/topCast/voteAverage/voteCount/originalLanguage) are backfilled
    // over hours; an un-healed row omits them. The schema must default the arrays to [] and the
    // scalars to null so the UI still parses and simply renders no rating/credits rows.
    const legacy = {
      titleId: "t1",
      title: "Arrival",
      kind: "movie",
      year: 2016,
      runtimeMinutes: 116,
      genres: ["Science Fiction"],
      overview: "A linguist makes first contact.",
      posterUrl: null,
      tmdbUrl: null,
      imdbUrl: null,
    };
    vi.stubGlobal("fetch", mockFetch(legacy));
    const detail = await api.titleDetail("t1");
    expect(detail.directors).toEqual([]);
    expect(detail.topCast).toEqual([]);
    expect(detail.voteAverage).toBeNull();
    expect(detail.voteCount).toBeNull();
    expect(detail.originalLanguage).toBeNull();
  });
});
