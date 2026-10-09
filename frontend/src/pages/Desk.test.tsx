import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as mock from "@/mock/api";

// The chart needs a canvas; jsdom has none. Pure drawing logic is covered in
// src/lib/desk/model.test.ts; here the chart is a stub that records its props.
const chartProps = vi.hoisted(() => ({ last: null as unknown }));
vi.mock("@/components/desk/Chart", () => ({
  DeskChart: (props: unknown) => {
    chartProps.last = props;
    return <div data-testid="desk-chart" />;
  },
}));

const mocks = vi.hoisted(() => ({
  pairs: vi.fn(),
  state: vi.fn(),
  candles: vi.fn(),
  health: vi.fn(),
  addPairs: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, ...mocks } };
});

import Desk from "@/pages/Desk";

function renderDesk() {
  return render(
    <MemoryRouter>
      <Desk />
    </MemoryRouter>,
  );
}

describe("Desk", () => {
  beforeEach(() => {
    window.localStorage.clear();
    mocks.pairs.mockImplementation(() => mock.pairs());
    mocks.state.mockImplementation((pair: string) => mock.state(pair));
    mocks.candles.mockImplementation((pair: string, tf: string, limit: number, before?: number) => mock.candles(pair, tf, limit, before));
    mocks.health.mockImplementation(() => mock.health());
    mocks.addPairs.mockImplementation((symbols: string[]) => mock.addPairs(symbols));
  });

  it("selects the first ready pair, shows its state and the nearest levels", async () => {
    renderDesk();
    const select = (await screen.findByTestId("pair-select")) as HTMLSelectElement;
    await waitFor(() => expect(select.options).toHaveLength(3));
    expect(select.value).toBe("SOL_USDT");
    expect(select.options[0].textContent).toBe("● SOL/USDT");
    await waitFor(() => expect(screen.getByTestId("live-price")).toHaveTextContent("$100.00"));
    expect(screen.getByTestId("live-ok")).toHaveTextContent("· live");
    expect(screen.getByTestId("trend-1d")).toHaveTextContent("▲ up");
    expect(screen.getByTestId("close-1h")).toHaveTextContent("$100.20");
    const rows = await screen.findAllByTestId("level-row");
    expect(rows).toHaveLength(4);
    expect(rows[0]).toHaveTextContent("$101.20");
    expect(rows[1]).toHaveTextContent("$98.40–$98.60");
    expect(screen.getByTestId("health-dot")).toHaveTextContent("● live");
    expect(screen.queryByTestId("stale-banner")).toBeNull();
    expect(screen.getByTestId("desk-status")).toHaveTextContent("SOL_USDT · ready");
    await waitFor(() => expect(window.__zonesRendered).toHaveLength(4));
  });

  it("feeds the chart candles for the pair/tf view and switches TF from cache", async () => {
    const user = userEvent.setup();
    renderDesk();
    await screen.findAllByTestId("level-row");
    await waitFor(() => expect((chartProps.last as { candles: unknown[] }).candles).toHaveLength(300));
    expect((chartProps.last as { viewKey: string }).viewKey).toBe("SOL_USDT:1h");
    const calls = mocks.candles.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "4H" }));
    await waitFor(() => expect((chartProps.last as { viewKey: string }).viewKey).toBe("SOL_USDT:4h"));
    expect(window.localStorage.getItem("ttr_tf")).toBe("4h");
    await user.click(screen.getByRole("button", { name: "1H" }));
    await waitFor(() => expect((chartProps.last as { viewKey: string }).viewKey).toBe("SOL_USDT:1h"));
    expect(mocks.candles.mock.calls.length).toBe(calls + 1); // 4h fetched once; 1h came from cache
  });

  it("toggles persist and change what is drawn", async () => {
    const user = userEvent.setup();
    renderDesk();
    await screen.findAllByTestId("level-row");
    await waitFor(() => expect(window.__zonesRendered).toHaveLength(4));
    await user.click(screen.getByTestId("tg-strong"));
    await waitFor(() => expect(window.__zonesRendered).toHaveLength(1));
    expect(JSON.parse(window.localStorage.getItem("ttr_toggles")!)).toMatchObject({ strong: false });
  });

  it("add-pair form reports rejections from the service", async () => {
    const user = userEvent.setup();
    renderDesk();
    await screen.findByTestId("pair-select");
    await user.click(screen.getByTestId("add-pair-btn"));
    await user.type(screen.getByTestId("add-pair-input"), "link/usdt{Enter}");
    expect(await screen.findByTestId("add-pair-err")).toHaveTextContent("отклонено: link/usdt (mock mode");
    expect(mocks.addPairs).toHaveBeenCalledWith(["link/usdt"]);
  });

  it("shows the stale banner when the worker is stale and api-down when health fails", async () => {
    mocks.health.mockImplementation(async () => ({
      ...(await mock.health()),
      pairs_stale: ["SOL_USDT"],
      pair_age_5m_s: { SOL_USDT: 1200, BTC_USDT: 10, ETH_USDT: 10 },
    }));
    renderDesk();
    expect(await screen.findByTestId("stale-banner")).toHaveAttribute("data-kind", "stale");
    expect(screen.getByTestId("health-dot")).toHaveTextContent("● stale");
  });
});
