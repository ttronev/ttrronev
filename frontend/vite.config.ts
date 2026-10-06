/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const repoRoot = path.resolve(here, "..");

// The app is served by FastAPI under /app (the legacy dashboard keeps / until
// Desk parity is verified). Under the Vite dev server /api is proxied to the
// local Docker stack, so the client always uses same-origin relative URLs.
export const DEFAULT_SERVICE_URL = "http://127.0.0.1:8090";

// Bind IPv4 explicitly: "localhost" resolves to ::1 on Node 17+/Windows, which
// made Playwright's 127.0.0.1 readiness probe wait forever.
const HOST = "127.0.0.1";

export default defineConfig(({ mode }) => {
  // Mock mode: `vite build --mode mock` (what the npm scripts use; works on
  // Windows too) or VITE_MOCK=1 in the environment. The client then serves
  // fixtures from src/mock instead of calling /api/v1.
  const mock = mode === "mock" || process.env.VITE_MOCK === "1";
  return {
    base: "/app/",
    plugins: [react()],
    // Not under vitest: tests toggle the flag with vi.stubEnv, which a static
    // define would override.
    define: mode === "test" ? {} : { "import.meta.env.VITE_MOCK": JSON.stringify(mock ? "1" : "") },
    resolve: {
      alias: {
        "@": path.resolve(here, "src"),
        // docs/plan/stages.json is the single source of the stage map; the
        // build bundles a snapshot of it (the Build page later prefers the
        // live copy from /api/v1/build/stages).
        "@plan": path.resolve(repoRoot, "docs", "plan"),
      },
    },
    server: {
      host: HOST,
      port: 5173,
      strictPort: true,
      fs: { allow: [repoRoot] },
      proxy: {
        "/api": { target: process.env.VITE_SERVICE_URL || DEFAULT_SERVICE_URL, changeOrigin: true },
      },
    },
    preview: { host: HOST, port: 4173, strictPort: true },
    build: { outDir: mock ? "dist-mock" : "dist", emptyOutDir: true, sourcemap: false },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      css: false,
    },
  };
});
