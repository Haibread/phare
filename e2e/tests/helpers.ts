import { type Page, expect } from "@playwright/test";

// Fixed credentials for the shared e2e account. The specs share one backend + DB and run serially,
// so the first spec to reach the gate registers the account (becoming admin) and the rest log in.
const E2E_EMAIL = "e2e@phare.test";
const E2E_PASSWORD = "e2e-password-123";

/** Get past the auth gate: register the first account (first-run setup) or log in afterwards.
 * Auth is closed by default and the token is in-memory only, so every page load shows the gate. */
export async function signIn(page: Page): Promise<void> {
  const setup = page.getByTestId("setup-gate");
  const login = page.getByTestId("login-gate");
  const cold = page.getByTestId("cold-start");
  const browse = page.getByTestId("tab-browse");
  await expect(setup.or(login).or(cold).or(browse)).toBeVisible({ timeout: 20_000 });
  if (await setup.isVisible()) {
    // First run: create the admin account.
    await page.getByTestId("auth-display-name").fill("E2E");
    await page.getByTestId("login-email").fill(E2E_EMAIL);
    await page.getByTestId("login-password").fill(E2E_PASSWORD);
    await page.getByTestId("auth-register-submit").click();
  } else if (await login.isVisible()) {
    await page.getByTestId("login-email").fill(E2E_EMAIL);
    await page.getByTestId("login-password").fill(E2E_PASSWORD);
    await page.getByTestId("login-submit").click();
  }
}

/** Sign in, then make sure sample data is loaded so the spec lands in the tabbed shell. Whichever
 * spec runs first hits the cold-start gate and seeds; later specs find the data already there. */
export async function ensureOnboarded(page: Page): Promise<void> {
  await page.goto("/");
  await signIn(page);
  const cold = page.getByTestId("cold-start");
  const browse = page.getByTestId("tab-browse");
  await expect(cold.or(browse)).toBeVisible({ timeout: 20_000 });
  if (await cold.isVisible()) {
    // Offline sample path: seeds the catalog + loads sample data, then the tabbed shell appears.
    await page.getByTestId("explore-sample").click();
    await expect(browse).toBeVisible({ timeout: 30_000 });
  }
}
