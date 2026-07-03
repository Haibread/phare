import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Attributions } from "./Attributions";

describe("Attributions (J2)", () => {
  it("keeps TMDB's required attribution sentence verbatim", () => {
    render(<Attributions />);
    // TMDB's terms mandate this exact wording — assert it hasn't drifted.
    expect(
      screen.getByText(/This product uses the TMDB API but is not endorsed or certified by/),
    ).toBeInTheDocument();
  });

  it("renders the official TMDB logo with descriptive alt text", () => {
    render(<Attributions />);
    const logo = screen.getByRole("img", { name: "The Movie Database (TMDB)" });
    expect(logo).toBeInTheDocument();
    // Vite inlines the small SVG as a data URI; assert it's the actual TMDB wordmark (its unique
    // viewBox) rather than a placeholder — the committed asset lives at src/assets/tmdb-logo.svg.
    const src = logo.getAttribute("src") ?? "";
    expect(src).toMatch(/^data:image\/svg\+xml/);
    expect(decodeURIComponent(src)).toContain("273.42 35.52");
  });
});
