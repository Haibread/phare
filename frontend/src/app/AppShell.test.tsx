import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { AppShell } from "./AppShell";

describe("AppShell landmarks (L4)", () => {
  it("wraps the route content in a <main> landmark", () => {
    render(
      <MemoryRouter>
        <Routes>
          <Route element={<AppShell profileId="p1" />}>
            <Route index element={<p>route content</p>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    // A single <main> landmark exists, and the header/nav are their own landmarks.
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByRole("navigation")).toBeInTheDocument();
    expect(screen.getByRole("banner")).toBeInTheDocument();
  });
});
