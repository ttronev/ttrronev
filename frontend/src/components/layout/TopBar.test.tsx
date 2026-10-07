import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { HealthV1 } from "@/lib/types";

const mocks = vi.hoisted(() => ({ health: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, health: mocks.health } };
});

import { ApiError } from "@/lib/api";
import { TopBar } from "@/components/layout/TopBar";

function fakeHealth(over: Partial<HealthV1> = {}): HealthV1 {
  return {
    version: "1",
    generated_at: "2026-10-06T12:00:00Z",
    status: "ok",
    worker_alive: true,
    worker_phase: "running",
    auth_enabled: false,
    tfs: {},
    pairs_ready: 0,
    pairs_total: 0,
    pairs_stale: [],
    pair_age_5m_s: {},
    cycle_5m_s: null,
    rss_mb: null,
    stale_after_s: 900,
    warn_after_s: 300,
    ...over,
  };
}

function renderBar() {
  return render(
    <MemoryRouter initialEntries={["/admin/logs"]}>
      <TopBar />
    </MemoryRouter>,
  );
}

describe("TopBar", () => {
  beforeEach(() => {
    mocks.health.mockReset();
  });

  it("shows the page title", async () => {
    mocks.health.mockResolvedValue(fakeHealth());
    renderBar();
    expect(screen.getByTestId("topbar-title")).toHaveTextContent("Logs");
  });

  it("shows the persistent dev-mode banner when the service has no auth", async () => {
    mocks.health.mockResolvedValue(fakeHealth({ auth_enabled: false }));
    renderBar();
    expect(await screen.findByTestId("dev-mode-banner")).toHaveTextContent("dev mode, no auth");
  });

  it("shows no dev-mode banner when auth is on", async () => {
    mocks.health.mockResolvedValue(fakeHealth({ auth_enabled: true }));
    renderBar();
    await waitFor(() => expect(mocks.health).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId("dev-mode-banner")).toBeNull());
    expect(screen.queryByTestId("auth-required-pill")).toBeNull();
  });

  it("asks for a token when the service answers 401", async () => {
    mocks.health.mockRejectedValue(new ApiError(401, "/api/v1/health"));
    renderBar();
    expect(await screen.findByTestId("auth-required-pill")).toHaveTextContent("token required");
    expect(screen.queryByTestId("dev-mode-banner")).toBeNull();
  });
});
