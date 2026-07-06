/** Render the light markdown the LLM sprinkles into explanations/summaries as React nodes.
 *
 * LLM prose arrives as plain strings that sometimes carry emphasis markers — `*Zero Dark Thirty*`,
 * `**bold**` — and the occasional stray artifact. We render those to real <em>/<strong> nodes rather
 * than dumping the raw asterisks on screen, and strip other markdown noise so the text reads clean.
 *
 * Deliberately tiny and safe: we parse the string into typed tokens and emit React elements — no
 * `dangerouslySetInnerHTML`, so there's no HTML-injection surface. Only inline emphasis is handled;
 * this is not a general markdown engine (no links, lists, code blocks). Unclosed or nested markers
 * degrade to plain text instead of throwing. */

import type { JSX, ReactNode } from "react";

/** Strip markdown artifacts that carry no inline-emphasis meaning, so they don't render literally:
 *  - heading hashes at the start of a line (`## Title` → `Title`)
 *  - list bullets at the start of a line (`- item` / `+ item` → `item`)
 *  - inline code backticks (kept as their content: `` `Dune` `` → `Dune`)
 * Emphasis markers (`*` / `**` / `_`) are left in place for the tokenizer below to interpret. */
function stripArtifacts(text: string): string {
  return text
    .replace(/^\s{0,3}#{1,6}\s+/gm, "") // ATX headings
    .replace(/^\s{0,3}[-+]\s+/gm, "") // list bullets (dash/plus; `*` handled by emphasis pass)
    .replace(/`([^`]*)`/g, "$1"); // inline code → its content
}

/** One emphasis marker we recognise, longest first so `**` wins over `*`. `_`/`__` alias `*`/`**`. */
const MARKERS: ReadonlyArray<{ token: string; tag: "strong" | "em" }> = [
  { token: "**", tag: "strong" },
  { token: "__", tag: "strong" },
  { token: "*", tag: "em" },
  { token: "_", tag: "em" },
];

/** Parse inline emphasis into React nodes. A marker only opens a span when a matching closing marker
 * exists later in the string; an unmatched marker is emitted as literal text (so `3 * 4` and a
 * dangling `*note` stay readable). One level of nesting is resolved recursively over a strictly
 * shrinking substring, so it always terminates. */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let buffer = "";
  let i = 0;
  let key = 0;

  const flush = () => {
    if (buffer !== "") {
      nodes.push(buffer);
      buffer = "";
    }
  };

  while (i < text.length) {
    const marker = MARKERS.find((m) => text.startsWith(m.token, i));
    if (marker) {
      const closeAt = text.indexOf(marker.token, i + marker.token.length);
      // Require non-empty content between the markers, else `**` on its own would swallow the rest.
      if (closeAt > i + marker.token.length) {
        flush();
        const inner = text.slice(i + marker.token.length, closeAt);
        const Tag = marker.tag;
        nodes.push(
          <Tag key={`${keyPrefix}-${key++}`}>{renderInline(inner, `${keyPrefix}-${key}`)}</Tag>,
        );
        i = closeAt + marker.token.length;
        continue;
      }
      // No closing marker — treat this one as literal characters.
      buffer += text.slice(i, i + marker.token.length);
      i += marker.token.length;
      continue;
    }
    buffer += text[i];
    i += 1;
  }
  flush();
  return nodes;
}

/** Convert LLM markdown-ish text to React nodes: strip stray artifacts, then resolve `*`/`**`
 * emphasis to <em>/<strong>. Safe on partial input (a mid-stream chat delta) — an unclosed marker
 * renders as plain text until its closer arrives. */
export function renderRichText(text: string): ReactNode {
  return renderInline(stripArtifacts(text), "rt");
}

/** Convenience wrapper so callers can drop `<RichText text={why} />` into JSX. */
export function RichText({ text }: { text: string }): JSX.Element {
  return <>{renderRichText(text)}</>;
}
