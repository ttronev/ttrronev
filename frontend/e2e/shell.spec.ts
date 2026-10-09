// Smoke test of the BUILT app (dist-mock served by `vite preview` under
// /app/). It checks the product contract of ТЗ-B0 §5/§7: the 13 sidebar
// entries in order, the Admin group last, and the mock-mode banners carrying
// the status text from docs/plan/stages.json (the fixture the build bundles).
import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

interface Stage {
  id: string;
  status: string;
}
const stagesFile = JSON.parse(readFileSync(new URL("../../docs/plan/stages.json", import.meta.url), "utf8")) as {
  stages: Stage[];
};

const ORDER = [
  "Desk", "Strategies", "Backtests", "Bots", "Live log", "Connections", "Chat", "Account",
  "Health", "Logs", "Build", "Agent log", "Settings",
];
const ADMIN = ORDER.slice(8);
const LIVE = ["Desk", "Health", "Logs", "Build", "Settings"];
const PLANNED: Record<string, string> = {
  Strategies: "B11", Backtests: "B11", Bots: "B12", "Live log": "B6",
  Connections: "B12", Chat: "B8", Account: "B13", "Agent log": "B8",
};

function expectedBanner(stageId: string): string {
  const stage = stagesFile.stages.find((s) => s.id === stageId);
  if (!stage) throw new Error(`fixture stages.json has no stage ${stageId}`);
  return `Mock data — real data arrives in stage ${stageId} — status: ${stage.status}`;
}

test("the sidebar lists the 13 entries in order, Admin group last", async ({ page }) => {
  await page.goto("./");
  await expect(page.getByTestId("sidebar")).toBeVisible();
  const labels = (await page.getByTestId("nav-entry").allTextContents()).map((s) => s.trim());
  expect(labels).toEqual(ORDER);

  const admin = page.getByTestId("sidebar-group-admin");
  await expect(admin.getByTestId("sidebar-group-admin-label")).toHaveText("Admin");
  expect((await admin.getByTestId("nav-entry").allTextContents()).map((s) => s.trim())).toEqual(ADMIN);
  // The Admin block is the last child of the nav element.
  const isLast = await page.getByTestId("sidebar").evaluate(
    (nav) => nav.lastElementChild?.getAttribute("data-testid") === "sidebar-group-admin",
  );
  expect(isLast).toBe(true);
});

test("the build is in mock mode (fixtures, no service) and shows the dev-mode banner", async ({ page }) => {
  await page.goto("./");
  await expect(page.getByTestId("mock-mode-pill")).toHaveText("mock mode");
  // The mock health fixture reports auth_enabled: false, as a dev service does.
  await expect(page.getByTestId("dev-mode-banner")).toHaveText("dev mode, no auth");
});

for (const [label, stageId] of Object.entries(PLANNED)) {
  test(`${label}: mock banner carries the status of ${stageId} from stages.json`, async ({ page }) => {
    await page.goto("./");
    await page.getByTestId("nav-entry").filter({ hasText: new RegExp(`^${label}$`) }).click();
    await expect(page.getByTestId("page-title")).toHaveText(label);
    const banner = page.getByTestId("mock-banner");
    await expect(banner).toHaveAttribute("data-stage", stageId);
    await expect(banner).toHaveText(expectedBanner(stageId));
  });
}

for (const label of LIVE) {
  test(`${label}: live page, no mock banner`, async ({ page }) => {
    await page.goto("./");
    await page.getByTestId("nav-entry").filter({ hasText: new RegExp(`^${label}$`) }).click();
    await expect(page.getByTestId("page-title")).toHaveText(label);
    await expect(page.getByTestId("mock-banner")).toHaveCount(0);
  });
}

test("deep links under /app/ resolve (SPA fallback)", async ({ page }) => {
  await page.goto("./admin/health");
  await expect(page.getByTestId("page-title")).toHaveText("Health");
  await page.goto("./no-such-page");
  await expect(page.getByTestId("page-title")).toHaveText("Page not found");
});
