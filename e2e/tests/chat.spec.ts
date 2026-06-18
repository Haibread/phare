import { expect, test } from "@playwright/test";
import { ensureOnboarded } from "./helpers";

test("chat agent replies with mood-filtered suggestions", async ({ page }) => {
  await ensureOnboarded(page);
  await page.getByTestId("tab-chat").click();

  // Keyword intent parsing runs offline (no LLM key): "funny" -> Comedy, "short" -> <=100 min.
  await page.getByTestId("chat-input").fill("something funny and short");
  await page.getByTestId("chat-send").click();

  // The user turn echoes, and the agent replies with at least one suggestion.
  await expect(page.getByTestId("chat-user")).toContainText("something funny and short");
  const agent = page.getByTestId("chat-agent");
  await expect(agent).toBeVisible({ timeout: 20_000 });
  await expect(agent.getByTestId("chat-item").first()).toBeVisible();
});

test("the app stays open when no password is set", async ({ page }) => {
  // Default stack runs without AUTH_PASSWORD, so /me reports open mode and no gate appears.
  await page.goto("/");
  await expect(page.getByTestId("login-gate")).toHaveCount(0);
  await expect(
    page.getByTestId("cold-start").or(page.getByTestId("tab-browse")),
  ).toBeVisible({ timeout: 20_000 });
});
