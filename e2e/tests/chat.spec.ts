import { expect, test } from "@playwright/test";
import { ensureOnboarded } from "./helpers";

test("chat agent replies to a tapped suggestion with mood-filtered picks", async ({ page }) => {
  await ensureOnboarded(page);
  await page.getByTestId("tab-chat").click();

  // The empty state greets and offers starter suggestions.
  await expect(page.getByTestId("chat-greeting")).toBeVisible();
  const starter = page.getByTestId("chat-suggestion").first();
  await expect(starter).toBeVisible();

  // Tapping a starter ("something funny and short") sends it; keyword parsing runs offline.
  await starter.click();
  await expect(page.getByTestId("chat-user").first()).toBeVisible();
  const agent = page.getByTestId("chat-agent").first();
  await expect(agent).toBeVisible({ timeout: 20_000 });
  await expect(agent.getByTestId("chat-item").first()).toBeVisible();

  // After a reply, follow-up suggestions are offered to keep the conversation going.
  await expect(page.getByTestId("chat-suggestion").first()).toBeVisible();
});

test("typing a mood also works", async ({ page }) => {
  await ensureOnboarded(page);
  await page.getByTestId("tab-chat").click();

  await page.getByTestId("chat-input").fill("something funny and short");
  await page.getByTestId("chat-send").click();

  await expect(page.getByTestId("chat-user").first()).toContainText("something funny and short");
  await expect(page.getByTestId("chat-agent").first()).toBeVisible({ timeout: 20_000 });
});

test("the app stays open when no password is set", async ({ page }) => {
  // Default stack runs without AUTH_PASSWORD, so /me reports open mode and no gate appears.
  await page.goto("/");
  await expect(page.getByTestId("login-gate")).toHaveCount(0);
  await expect(
    page.getByTestId("cold-start").or(page.getByTestId("tab-browse")),
  ).toBeVisible({ timeout: 20_000 });
});
