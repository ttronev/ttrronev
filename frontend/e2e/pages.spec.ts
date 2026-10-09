// Live pages (Health, Logs, Build, Settings) and the planned pages on the
// mock build.
import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

const stagesFile = JSON.parse(readFileSync(new URL("../../docs/plan/stages.json", import.meta.url), "utf8")) as {
  stages: { id: string; status: string }[];
};

test("Health: service verdict, stats, timeframe and pair tables", async ({ page }) => {
  await page.goto("./admin/health");
  await expect(page.getByTestId("health-status")).toHaveAttribute("data-status", "ok");
  await expect(page.getByTestId("health-worker")).toHaveText("alive");
  await expect(page.getByTestId("health-cycle")).toHaveText("4.2 s");
  await expect(page.getByTestId("health-rss")).toHaveText("152 MB");
  await expect(page.getByTestId("health-latency")).toContainText("ms");
  await expect(page.getByTestId("tf-row")).toHaveCount(6);
  await expect(page.getByTestId("pair-row")).toHaveCount(3);
  await expect(page.getByTestId("last-updated")).toContainText("updated");
});

test("Logs: tail, filter, follow toggle", async ({ page }) => {
  await page.goto("./admin/logs");
  await expect(page.getByTestId("log-line")).toHaveCount(5);
  await page.getByTestId("log-filter").fill("5m pass");
  await expect(page.getByTestId("log-line")).toHaveCount(1);
  await expect(page.getByTestId("log-count")).toContainText("showing 1 of 5");
  await expect(page.getByTestId("log-follow")).toBeChecked();
  await page.getByTestId("log-follow").uncheck();
  await expect(page.getByTestId("log-follow")).not.toBeChecked();
});

test("Build: the stage table and the Now strip come from stages.json", async ({ page }) => {
  await page.goto("./admin/build");
  await expect(page.getByTestId("stage-row")).toHaveCount(stagesFile.stages.length);
  const inProgress = stagesFile.stages.filter((s) => s.status === "in_progress").map((s) => s.id);
  const now = await page.getByTestId("now-stage").allTextContents();
  expect(now.map((t) => t.split(" · ")[0])).toEqual(inProgress);
});

test("Settings: token stored masked, test connection answers", async ({ page }) => {
  await page.goto("./admin/settings");
  await page.getByTestId("api-token").fill("secret-token-1234");
  await page.getByTestId("settings-save").click();
  await expect(page.getByTestId("api-token-stored")).toContainText("••••••••1234");
  await expect(page.getByTestId("api-token-stored")).not.toContainText("secret");
  await page.getByTestId("settings-test").click();
  await expect(page.getByTestId("settings-test-result")).toHaveAttribute("data-ok", "1");
  await expect(page.getByTestId("settings-test-result")).toContainText("app 0.1.0");
  await page.getByTestId("settings-clear-token").click();
  await expect(page.getByTestId("api-token-stored")).toContainText("none");
});

const PLANNED: [string, string, string][] = [
  ["strategies", "strategy-row", "B11"],
  ["backtests", "backtest-row", "B11"],
  ["bots", "bot-row", "B12"],
  ["live-log", "livelog-row", "B6"],
  ["connections", "connection-row", "B12"],
  ["chat", "chat-message", "B8"],
  ["account", "account-tokens", "B13"],
  ["admin/agent-log", "agentlog-row", "B8"],
];

for (const [path, rowId, stage] of PLANNED) {
  test(`${path}: layout on mock data under the ${stage} banner`, async ({ page }) => {
    await page.goto(`./${path}`);
    await expect(page.getByTestId("mock-banner")).toHaveAttribute("data-stage", stage);
    expect(await page.getByTestId(rowId).count()).toBeGreaterThan(0);
    for (const b of await page.getByTestId("planned-action").all()) await expect(b).toBeDisabled();
  });
}
