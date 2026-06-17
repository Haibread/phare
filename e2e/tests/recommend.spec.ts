import { expect, test } from "@playwright/test";

test("create a profile, seed data + catalog, and get recommendations", async ({ page }) => {
  await page.goto("/");

  const name = `E2E Rec ${Date.now()}`;
  await page.getByTestId("new-profile-name").fill(name);
  await page.getByTestId("create-profile").click();
  await expect(page.getByTestId("status")).toContainText("Created profile");

  await page.getByTestId("load-sample").click();
  await expect(page.getByTestId("status")).toContainText("Loaded sample data");

  await page.getByTestId("load-catalog").click();
  await expect(page.getByTestId("status")).toContainText("Loaded sample catalog");

  // The engine lazily embeds the catalog (local hash provider) on the first request, so this is
  // a full offline pass through candidate generation -> re-ranker -> templated explanations.
  await page.getByTestId("refresh-recs").click();
  await expect(page.getByTestId("status")).toContainText("Loaded");

  // The product row is present and its cards carry explanations.
  const youMightLike = page.locator('[data-row-key="you_might_like"]');
  await expect(youMightLike).toBeVisible();
  const firstCard = youMightLike.getByTestId("rec-card").first();
  await expect(firstCard).toBeVisible();
  await expect(firstCard.locator(".explanation")).not.toBeEmpty();
});
