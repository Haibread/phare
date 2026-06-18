import { type Page, expect } from "@playwright/test";

/** The specs share one backend + database. Whichever spec runs first hits the cold-start gate and
 * loads sample data; later specs find the data already there and land straight in the shell. This
 * makes each spec independent of run order. */
export async function ensureOnboarded(page: Page): Promise<void> {
  await page.goto("/");
  const cold = page.getByTestId("cold-start");
  const browse = page.getByTestId("tab-browse");
  await expect(cold.or(browse)).toBeVisible({ timeout: 20_000 });
  if (await cold.isVisible()) {
    // Offline sample path: seeds the catalog + loads sample data, then the tabbed shell appears.
    await page.getByTestId("explore-sample").click();
    await expect(browse).toBeVisible({ timeout: 30_000 });
  }
}
