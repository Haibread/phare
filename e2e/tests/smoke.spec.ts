import { expect, test } from "@playwright/test";
import { ensureOnboarded } from "./helpers";

test("onboards from cold start into the browsing shell", async ({ page }) => {
  await ensureOnboarded(page);

  // The shell's primary nav is present…
  await expect(page.getByTestId("tab-browse")).toBeVisible();
  await expect(page.getByTestId("tab-chat")).toBeVisible();
  await expect(page.getByTestId("tab-profile")).toBeVisible();

  // …and the product row renders with cards.
  const row = page.locator('[data-row-key="you_might_like"]');
  await expect(row).toBeVisible({ timeout: 20_000 });
  await expect(row.getByTestId("rec-card").first()).toBeVisible();
});
