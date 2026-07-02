import { expect, test } from "@playwright/test";
import { ensureOnboarded } from "./helpers";

test('"not interested" removes a card and undo brings it back', async ({ page }) => {
  await ensureOnboarded(page);

  const row = page.locator('[data-row-key="you_might_like"]');
  await expect(row).toBeVisible({ timeout: 20_000 });
  const firstCard = row.getByTestId("rec-card").first();
  await expect(firstCard).toBeVisible();

  // Reject the pick from the card. It leaves the row for an undo slot.
  await firstCard.getByTestId("not-interested").click();
  await expect(firstCard.getByTestId("rec-card-removed")).toBeVisible();
  await expect(firstCard.getByTestId("not-interested")).toHaveCount(0);

  // Undo restores the full card (and reverses the write, leaving the shared DB as we found it).
  const undo = firstCard.getByTestId("undo-not-interested");
  await expect(undo).toBeEnabled({ timeout: 10_000 });
  await undo.click();
  await expect(firstCard.getByTestId("not-interested")).toBeVisible();
  await expect(firstCard.getByTestId("rec-card-removed")).toHaveCount(0);
});
