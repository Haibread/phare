import { describe, expect, it } from "vitest";
import { translateGenre, translateTasteTerm } from "./tasteVocab";

describe("translateGenre", () => {
  it("translates a known genre into French", () => {
    expect(translateGenre("Drama", "fr")).toBe("Drame");
    expect(translateGenre("Fantasy", "fr")).toBe("Fantastique");
    expect(translateGenre("Science Fiction", "fr")).toBe("Science-Fiction");
  });

  it("handles a region-qualified locale tag", () => {
    expect(translateGenre("Horror", "fr-FR")).toBe("Horreur");
  });

  it("is case-insensitive for lower-cased keys (the extractor sometimes lower-cases)", () => {
    expect(translateGenre("drama", "fr")).toBe("Drame");
  });

  it("falls back to the raw name for an unknown genre", () => {
    expect(translateGenre("Telenovela", "fr")).toBe("Telenovela");
  });

  it("returns the name unchanged for English (identity)", () => {
    expect(translateGenre("Drama", "en")).toBe("Drama");
    expect(translateGenre("Fantasy", "en")).toBe("Fantasy");
  });

  it("returns the name unchanged for a language with no table", () => {
    expect(translateGenre("Drama", "de")).toBe("Drama");
  });
});

describe("translateTasteTerm", () => {
  it("also covers affinity descriptors, not just genres", () => {
    expect(translateTasteTerm("slow-burn", "fr")).toBe("rythme lent");
  });
});
