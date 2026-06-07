import { expect, test } from "@playwright/test";

test("create a profile, load sample data, and see history", async ({ page }) => {
  await page.goto("/");

  // Unique name so repeated runs against a persistent DB don't collide.
  const name = `E2E ${Date.now()}`;
  await page.getByTestId("new-profile-name").fill(name);
  await page.getByTestId("create-profile").click();

  await expect(page.getByTestId("status")).toContainText("Created profile");
  await expect(page.getByTestId("profile-select")).toContainText(name);

  await page.getByTestId("load-sample").click();

  await expect(page.getByTestId("status")).toContainText("Loaded sample data");
  await expect(page.getByTestId("history-row")).toHaveCount(6);
  await expect(page.getByTestId("history-table")).toContainText("Dune");
  await expect(page.getByTestId("history-table")).toContainText("Severance");
});
