import { expect, test } from "@playwright/test";
import { ensureOnboarded } from "./helpers";

test("search finds a title from the header", async ({ page }) => {
  await ensureOnboarded(page);

  await page.getByTestId("open-search").click();
  const input = page.getByTestId("search-input");
  await expect(input).toBeVisible();

  // "dune" matches the sample history's Dune title (local search runs offline, no TMDB key).
  await input.fill("dune");
  const results = page.getByTestId("search-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results.getByTestId("rec-card").first()).toBeVisible();
});
