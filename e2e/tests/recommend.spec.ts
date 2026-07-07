import { expect, test } from "@playwright/test";
import { ensureOnboarded } from "./helpers";

test("recommendations show honest fit labels and reserve swing picks", async ({ page }) => {
  await ensureOnboarded(page);

  // The product row renders with at least one card.
  const row = page.locator('[data-row-key="you_might_like"]');
  await expect(row).toBeVisible({ timeout: 20_000 });
  await expect(row.getByTestId("rec-card").first()).toBeVisible();

  // A non-swing card carries a worded fit label (the gauge). Swings wear a badge instead of a gauge,
  // and a thin row can be a lone swing, so assert the label on a card that actually has one — the
  // "because you watched" rows are full of taste-fit cards.
  await expect(page.getByTestId("fit").first()).toBeVisible();

  // A personalized "because you watched X" row renders, anchored on a loved title.
  const because = page.locator('[data-row-key^="because:"]').first();
  await expect(because).toBeVisible();
  await expect(because.locator("h3")).toContainText("Because you watched");

  // At least one swing pick is reserved and flagged as a deliberate stretch.
  await expect(page.getByTestId("swing-badge").first()).toBeVisible();

  // The closed-loop conversion read-out lives on the Profile tab; fresh data has none matured yet.
  await page.getByTestId("tab-profile").click();
  await expect(page.getByTestId("conversion")).toContainText("Not enough history yet");
});
