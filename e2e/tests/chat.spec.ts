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
  // A returned pick renders as "chat-item", or "chat-item-cited" when the reply names it — accept
  // either so the assertion doesn't flake on whether the pick was cited in the sentence.
  await expect(
    agent.locator('[data-testid="chat-item"], [data-testid="chat-item-cited"]').first(),
  ).toBeVisible();

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

test("the app is closed by default and requires signing in", async ({ page }) => {
  // Multi-user auth is mandatory now: a fresh context (no token) must hit the auth gate — either
  // first-run setup or the login form — and never the app shell directly.
  await page.goto("/");
  await expect(
    page.getByTestId("setup-gate").or(page.getByTestId("login-gate")),
  ).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("tab-browse")).toHaveCount(0);
});
