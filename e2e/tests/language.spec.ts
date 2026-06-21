import { expect, test } from "@playwright/test";
import { ensureOnboarded, signIn } from "./helpers";

test("the header switcher flips the UI language and persists the choice", async ({ page }) => {
  await ensureOnboarded(page);

  // Chromium defaults to English, so the tabs start in English.
  await expect(page.getByTestId("tab-browse")).toContainText("Browse");

  // Switching to French re-renders the chrome immediately (i18next, no reload).
  await page.getByTestId("language-selector").selectOption("fr");
  await expect(page.getByTestId("tab-browse")).toContainText("Parcourir");
  await expect(page.getByTestId("tab-profile")).toContainText("Profil");

  // The choice is saved per browser.
  const stored = await page.evaluate(() => localStorage.getItem("phare.language"));
  expect(stored).toBe("fr");

  // …and toggling back to English restores the original labels.
  await page.getByTestId("language-selector").selectOption("en");
  await expect(page.getByTestId("tab-browse")).toContainText("Browse");
});

test("the saved language survives a reload and localises backend text", async ({ page }) => {
  await ensureOnboarded(page);

  await page.getByTestId("language-selector").selectOption("fr");
  await expect(page.getByTestId("tab-browse")).toContainText("Parcourir");

  // A fresh load reads the saved language and sends Accept-Language: fr, so the row titles the
  // backend builds come back in French too — proving the language flows end to end. The bearer
  // token is in-memory only (a deliberate security posture), so a reload drops it and shows the
  // gate in the saved language — sign back in, then assert the localised shell.
  await page.reload();
  await signIn(page);
  await expect(page.getByTestId("tab-browse")).toContainText("Parcourir");
  const row = page.locator('[data-row-key="you_might_like"]');
  await expect(row).toBeVisible({ timeout: 20_000 });
  await expect(row).toContainText("Pourrait vous plaire");
});
