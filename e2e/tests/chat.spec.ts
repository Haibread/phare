import { expect, test } from "@playwright/test";

test("chat agent replies with mood-filtered suggestions", async ({ page }) => {
  await page.goto("/");

  const name = `E2E Chat ${Date.now()}`;
  await page.getByTestId("new-profile-name").fill(name);
  await page.getByTestId("create-profile").click();
  await expect(page.getByTestId("status")).toContainText("Created profile");

  await page.getByTestId("load-sample").click();
  await expect(page.getByTestId("status")).toContainText("Loaded sample data");
  await page.getByTestId("load-catalog").click();
  await expect(page.getByTestId("status")).toContainText("Loaded sample catalog");

  // Keyword intent parsing runs offline (no LLM key): "funny" -> Comedy, "short" -> <=100 min.
  await page.getByTestId("chat-input").fill("something funny and short");
  await page.getByTestId("chat-send").click();

  // The user turn echoes, and the agent replies with a comedy-flavoured suggestion.
  await expect(page.getByTestId("chat-user")).toContainText("something funny and short");
  const agent = page.getByTestId("chat-agent");
  await expect(agent).toBeVisible();
  await expect(agent).toContainText("comedy");
  await expect(agent.getByTestId("rec-card").first()).toBeVisible();
});

test("the auth status path keeps the app open when no password is set", async ({ page }) => {
  // Default stack runs without AUTH_PASSWORD, so /me reports open mode and no gate appears.
  await page.goto("/");
  await expect(page.getByTestId("login-gate")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Phare" })).toBeVisible();
});
