import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RichText } from "./richText";

/** Render to a container and read the resulting HTML, so we assert on the real emitted nodes
 * (<em>/<strong>) rather than the parser's internals. */
function html(text: string): string {
  const { container } = render(<RichText text={text} />);
  return container.innerHTML;
}

describe("RichText", () => {
  it("renders single asterisks as emphasis, stripping the markers", () => {
    expect(html("watch *Zero Dark Thirty* tonight")).toBe(
      "watch <em>Zero Dark Thirty</em> tonight",
    );
  });

  it("renders double asterisks as strong", () => {
    expect(html("a **hard** pick")).toBe("a <strong>hard</strong> pick");
  });

  it("treats underscores as emphasis aliases", () => {
    expect(html("_soft_ and __loud__")).toBe("<em>soft</em> and <strong>loud</strong>");
  });

  it("leaves an unclosed marker as plain text (no crash)", () => {
    expect(html("a lone * asterisk")).toBe("a lone * asterisk");
    expect(html("*only an opener")).toBe("*only an opener");
  });

  it("does not turn arithmetic into emphasis when there is no closing pair", () => {
    expect(html("3 * 4 is 12")).toBe("3 * 4 is 12");
  });

  it("resolves nested emphasis without throwing", () => {
    expect(html("**bold with *italic* inside**")).toBe(
      "<strong>bold with <em>italic</em> inside</strong>",
    );
  });

  it("degrades a dangling nested marker to plain text", () => {
    // The outer strong closes; the inner unmatched `*` stays literal — must not crash.
    expect(html("**bold *dangling**")).toBe("<strong>bold *dangling</strong>");
  });

  it("strips heading hashes and list bullets", () => {
    expect(html("## Heading")).toBe("Heading");
    expect(html("- a bullet")).toBe("a bullet");
  });

  it("unwraps inline code backticks to their content", () => {
    expect(html("try `Dune` next")).toBe("try Dune next");
  });

  it("handles empty and marker-only input", () => {
    expect(html("")).toBe("");
    expect(html("**")).toBe("**");
    expect(html("****")).toBe("****");
  });

  it("leaves plain text untouched", () => {
    expect(html("just a normal sentence.")).toBe("just a normal sentence.");
  });
});
