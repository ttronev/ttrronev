import { defineConfig, devices } from "@playwright/test";

// Smoke test of the BUILT app in mock mode: no service needed. `webServer`
// builds dist-mock and serves it with `vite preview` under /app/ (the same
// base path FastAPI uses), so deep links are exercised the way production
// serves them.
const PORT = 4173;
const BASE = `http://127.0.0.1:${PORT}/app/`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  use: { baseURL: BASE, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "npm run build:mock && npm run preview:mock",
    url: BASE,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
});
