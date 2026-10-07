import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import App from "@/App";
import { BUNDLED_STAGES, mockBannerText } from "@/lib/stages";

// The top bar polls /api/v1/health; routing tests do not need a service.
vi.mock("@/lib/useHealth", () => ({
  useHealth: () => ({ health: null, error: null, lastOkAt: null }),
}));
// The Desk chart needs a canvas; jsdom has none (the chart is covered by the
// Playwright smoke test on the mock build).
vi.mock("@/components/desk/Chart", () => ({ DeskChart: () => <div data-testid="desk-chart" /> }));

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("routes", () => {
  it("/ is Desk, a live page without a mock banner", () => {
    renderAt("/");
    expect(screen.getByTestId("page-title")).toHaveTextContent("Desk");
    expect(screen.queryByTestId("mock-banner")).toBeNull();
  });

  it("/admin/health deep-links to Health", () => {
    renderAt("/admin/health");
    expect(screen.getByTestId("page-title")).toHaveTextContent("Health");
    expect(screen.getByTestId("topbar-title")).toHaveTextContent("Health");
    expect(screen.queryByTestId("mock-banner")).toBeNull();
  });

  it("/strategies shows the mock banner with the status from stages.json", () => {
    renderAt("/strategies");
    expect(screen.getByTestId("page-title")).toHaveTextContent("Strategies");
    expect(screen.getByTestId("mock-banner")).toHaveTextContent(mockBannerText("B11", BUNDLED_STAGES.stages));
  });

  it("/admin/agent-log is a planned page in the Admin group", () => {
    renderAt("/admin/agent-log");
    expect(screen.getByTestId("page-title")).toHaveTextContent("Agent log");
    expect(screen.getByTestId("mock-banner")).toHaveAttribute("data-stage", "B8");
  });

  it("an unknown address shows Page not found inside the shell", () => {
    renderAt("/no-such-page");
    expect(screen.getByTestId("page-title")).toHaveTextContent("Page not found");
    expect(screen.getByTestId("sidebar")).toBeInTheDocument();
  });
});
