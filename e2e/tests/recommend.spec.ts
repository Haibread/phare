import { expect, test } from "@playwright/test";
import { ensureOnboarded } from "./helpers";

test("recommendations show honest fit labels and reserve swing picks", async ({ page }) => {
  await ensureOnboarded(page);

  // The product row is populated and every card carries a worded fit label.
  const row = page.locator('[data-row-key="you_might_like"]');
  await expect(row).toBeVisible({ timeout: 20_000 });
  const firstCard = row.getByTestId("rec-card").first();
  await expect(firstCard).toBeVisible();
  await expect(firstCard.getByTestId("fit")).toBeVisible();

  // At least one swing pick is reserved and flagged as a deliberate stretch.
  await expect(page.getByTestId("swing-badge").first()).toBeVisible();

  // The closed-loop conversion read-out lives on the Profile tab; fresh data has none matured yet.
  await page.getByTestId("tab-profile").click();
  await expect(page.getByTestId("conversion")).toContainText("not enough history yet");
});
