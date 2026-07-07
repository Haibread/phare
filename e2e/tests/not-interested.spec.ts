import { expect, test } from "@playwright/test";
import { ensureOnboarded } from "./helpers";

test('"not interested" removes a title from the detail sheet and undo restores it', async ({
  page,
}) => {
  await ensureOnboarded(page);

  // The "not interested" control lives in the detail sheet now (round 3 moved it off the card so a
  // stray tap can't fire the destructive signal). Open the first recommendation's sheet.
  const row = page.locator('[data-row-key="you_might_like"]');
  await expect(row).toBeVisible({ timeout: 20_000 });
  await row.getByTestId("rec-card-open").first().click();

  const sheet = page.getByTestId("title-detail");
  await expect(sheet).toBeVisible();

  // Reject the title. The control flips to a confirmation + undo in place.
  await sheet.getByTestId("detail-not-interested").click();
  await expect(sheet.getByTestId("detail-removed")).toBeVisible();
  await expect(sheet.getByTestId("detail-not-interested")).toHaveCount(0);

  // Undo reverses the write, leaving the shared DB as we found it.
  const undo = sheet.getByTestId("detail-undo");
  await expect(undo).toBeEnabled({ timeout: 10_000 });
  await undo.click();
  await expect(sheet.getByTestId("detail-not-interested")).toBeVisible();
  await expect(sheet.getByTestId("detail-removed")).toHaveCount(0);
});
