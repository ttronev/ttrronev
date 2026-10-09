// Desk smoke on the mock build: the real Lightweight Charts runs in Chromium
// against the fixtures, so this covers what jsdom cannot (canvas, markers,
// price lines) through the same acceptance hooks the legacy page exposes.
import { expect, test } from "@playwright/test";

declare global {
  interface Window {
    __zonesRendered?: { price: number; lo: number; hi: number; n: number }[];
    __levelsRendered?: number[];
  }
}

test.beforeEach(async ({ page }) => {
  await page.goto("./");
  await expect(page.getByTestId("page-title")).toHaveText("Desk");
});

test("the chart draws candles and the four mock zones on the default 1H view", async ({ page }) => {
  await expect(page.locator('[data-testid="desk-chart"] canvas').first()).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.__zonesRendered?.length ?? 0)).toBe(4);
  const zones = await page.evaluate(() => window.__zonesRendered);
  expect(zones?.map((z) => z.price)).toEqual([98.5, 101.2, 95, 106]);
  expect(zones?.[0]).toEqual({ price: 98.5, lo: 98.4, hi: 98.6, n: 2 });
  await expect(page.getByTestId("tf-btn").filter({ hasText: /^1H$/ })).toHaveClass(/bg-accent/);
});

test("state tiles and the nearest-levels table come from the service state", async ({ page }) => {
  await expect(page.getByTestId("live-price")).toHaveText("$100.00");
  await expect(page.getByTestId("live-ok")).toHaveText("· live");
  await expect(page.getByTestId("trend-1d")).toHaveText("▲ up");
  await expect(page.getByTestId("close-1h")).toHaveText("$100.20");
  const rows = page.getByTestId("level-row");
  await expect(rows).toHaveCount(4);
  await expect(rows.nth(0)).toContainText("$101.20");
  await expect(rows.nth(0)).toContainText("+1.20%");
  await expect(rows.nth(1)).toContainText("$98.40–$98.60");
  await expect(rows.nth(1)).toContainText("4H+1H");
  await expect(page.getByTestId("health-dot")).toHaveText("● live");
  await expect(page.getByTestId("desk-status")).toContainText("SOL_USDT · ready");
});

test("pair select lists the registry; timeframe and toggles change the drawing", async ({ page }) => {
  const select = page.getByTestId("pair-select");
  await expect(select.locator("option")).toHaveCount(3);
  await expect(select.locator("option").first()).toHaveText("● SOL/USDT");

  await page.getByTestId("tf-btn").filter({ hasText: /^4H$/ }).click();
  await expect.poll(() => page.evaluate(() => window.__zonesRendered?.length ?? 0)).toBe(3);
  await page.getByTestId("tf-btn").filter({ hasText: /^1H$/ }).click();
  await expect.poll(() => page.evaluate(() => window.__zonesRendered?.length ?? 0)).toBe(4);

  await page.getByTestId("tg-strong").uncheck();
  await expect.poll(() => page.evaluate(() => window.__zonesRendered?.length ?? 0)).toBe(1);
  await page.getByTestId("tg-strong").check();
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem("ttr_toggles"))).toContain('"strong":true');

  await select.selectOption("BTC_USDT");
  await expect(page.getByTestId("desk-status")).toContainText("BTC_USDT · ready");
});

test("the add-pair form surfaces the service's rejection", async ({ page }) => {
  await page.getByTestId("add-pair-btn").click();
  await page.getByTestId("add-pair-input").fill("link/usdt");
  await page.getByTestId("add-pair-go").click();
  await expect(page.getByTestId("add-pair-err")).toContainText("mock mode");
});
