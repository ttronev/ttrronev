import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as mock from "@/mock/api";
import { BUNDLED_STAGES } from "@/lib/stages";

const mocks = vi.hoisted(() => ({
  health: vi.fn(),
  pairs: vi.fn(),
  logsTail: vi.fn(),
  stages: vi.fn(),
  version: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, ...mocks } };
});

import { ApiError } from "@/lib/api";
import Account from "@/pages/Account";
import AgentLog from "@/pages/AgentLog";
import Backtests from "@/pages/Backtests";
import Bots from "@/pages/Bots";
import Build from "@/pages/Build";
import Chat from "@/pages/Chat";
import Connections from "@/pages/Connections";
import Health from "@/pages/Health";
import LiveLog from "@/pages/LiveLog";
import Logs from "@/pages/Logs";
import Settings from "@/pages/Settings";
import Strategies from "@/pages/Strategies";

const wrap = (el: React.ReactElement) => render(<MemoryRouter>{el}</MemoryRouter>);

beforeEach(() => {
  window.localStorage.clear();
  mocks.health.mockImplementation(() => mock.health());
  mocks.pairs.mockImplementation(() => mock.pairs());
  mocks.logsTail.mockImplementation((n: number) => mock.logsTail(n));
  mocks.stages.mockImplementation(() => mock.stages());
  mocks.version.mockImplementation(() => mock.version());
});

describe("Health", () => {
  it("shows the service verdict, the stats and both tables", async () => {
    wrap(<Health />);
    await waitFor(() => expect(screen.getByTestId("health-status")).toHaveAttribute("data-status", "ok"));
    expect(screen.getByTestId("health-status-text")).toHaveTextContent("ok");
    expect(screen.getByTestId("health-worker")).toHaveTextContent("alive");
    expect(screen.getByTestId("health-cycle")).toHaveTextContent("4.2 s");
    expect(screen.getByTestId("health-rss")).toHaveTextContent("152 MB");
    expect(screen.getByTestId("health-latency")).toHaveTextContent(/ms/);
    expect(screen.getByTestId("health-pairs")).toHaveTextContent("3 / 3");
    expect(screen.getAllByTestId("tf-row")).toHaveLength(6);
    await waitFor(() => expect(screen.getAllByTestId("pair-row")).toHaveLength(3));
    expect(screen.getByTestId("last-updated")).toHaveTextContent(/updated/);
  });

  it("goes red and shows the offline banner with the last fetch time when the service is away", async () => {
    mocks.health.mockRejectedValue(new TypeError("Failed to fetch"));
    mocks.pairs.mockRejectedValue(new TypeError("Failed to fetch"));
    wrap(<Health />);
    await waitFor(() => expect(screen.getByTestId("health-status")).toHaveAttribute("data-status", "down"));
    const offline = screen.getAllByTestId("offline")[0];
    expect(offline).toHaveTextContent("service unreachable");
    expect(offline).toHaveTextContent("last successful fetch never");
    expect(offline).toHaveTextContent("retrying every 15 s");
  });

  it("points at Settings on a 401 instead of calling it an outage", async () => {
    mocks.health.mockRejectedValue(new ApiError(401, "/api/v1/health"));
    wrap(<Health />);
    await waitFor(() => expect(screen.getAllByTestId("offline")[0]).toHaveAttribute("data-kind", "unauthorized"));
  });
});

describe("Logs", () => {
  it("tails the worker log, filters by text, and asks for the chosen number of lines", async () => {
    const user = userEvent.setup();
    wrap(<Logs />);
    await waitFor(() => expect(screen.getAllByTestId("log-line")).toHaveLength(5));
    expect(mocks.logsTail).toHaveBeenCalledWith(500, expect.anything());
    expect(screen.getByTestId("log-count")).toHaveTextContent("showing 5 of 5");
    await user.type(screen.getByTestId("log-filter"), "5m pass");
    await waitFor(() => expect(screen.getAllByTestId("log-line")).toHaveLength(1));
    expect(screen.getByTestId("log-count")).toHaveTextContent("showing 1 of 5");
    await user.selectOptions(screen.getByTestId("log-lines-select"), "2000");
    await waitFor(() => expect(mocks.logsTail).toHaveBeenCalledWith(2000, expect.anything()));
    expect(screen.getByTestId("log-follow")).toBeChecked();
  });
});

describe("Build", () => {
  it("lists every stage from the service and the in-progress ones in the Now strip", async () => {
    wrap(<Build />);
    await waitFor(() => expect(screen.getByTestId("last-updated")).toHaveTextContent("live from the service"));
    expect(screen.getAllByTestId("stage-row")).toHaveLength(BUNDLED_STAGES.stages.length);
    const now = within(screen.getByTestId("now-strip")).queryAllByTestId("now-stage");
    const inProgress = BUNDLED_STAGES.stages.filter((s) => s.status === "in_progress").map((s) => `${s.id} · ${s.name}`);
    expect(now.map((n) => n.textContent)).toEqual(inProgress);
  });

  it("falls back to the bundled snapshot when the service is away", async () => {
    mocks.stages.mockRejectedValue(new TypeError("Failed to fetch"));
    wrap(<Build />);
    await waitFor(() => expect(screen.getByTestId("offline")).toBeInTheDocument());
    expect(screen.getAllByTestId("stage-row")).toHaveLength(BUNDLED_STAGES.stages.length);
    expect(screen.getByTestId("last-updated")).toHaveTextContent("bundled snapshot");
  });
});

describe("Settings", () => {
  it("stores the URL and the token locally, shows the token masked, tests the connection", async () => {
    const user = userEvent.setup();
    wrap(<Settings />);
    expect(screen.getByTestId("api-token-stored")).toHaveTextContent("none");
    await user.type(screen.getByTestId("service-url"), "http://127.0.0.1:8090/");
    await user.type(screen.getByTestId("api-token"), "secret-token-1234");
    await user.click(screen.getByTestId("settings-save"));
    expect(window.localStorage.getItem("ttrronev.serviceUrl")).toBe("http://127.0.0.1:8090/");
    expect(window.localStorage.getItem("ttrronev.apiToken")).toBe("secret-token-1234");
    expect(screen.getByTestId("api-token-stored")).toHaveTextContent("••••••••1234");
    expect(screen.getByTestId("api-token-stored")).not.toHaveTextContent("secret");
    expect((screen.getByTestId("api-token") as HTMLInputElement).value).toBe("");

    await user.click(screen.getByTestId("settings-test"));
    await waitFor(() => expect(screen.getByTestId("settings-test-result")).toHaveAttribute("data-ok", "1"));
    expect(screen.getByTestId("settings-test-result")).toHaveTextContent("app 0.1.0");

    await user.click(screen.getByTestId("settings-clear-token"));
    expect(window.localStorage.getItem("ttrronev.apiToken")).toBeNull();
    expect(screen.getByTestId("settings-theme")).toHaveTextContent(/currently: (dark|light)/);
  });

  it("reports a rejected token and an unreachable service distinctly", async () => {
    const user = userEvent.setup();
    mocks.version.mockRejectedValueOnce(new ApiError(401, "/api/v1/version"));
    wrap(<Settings />);
    await user.click(screen.getByTestId("settings-test"));
    await waitFor(() => expect(screen.getByTestId("settings-test-result")).toHaveTextContent("rejected"));
    mocks.version.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await user.click(screen.getByTestId("settings-test"));
    await waitFor(() => expect(screen.getByTestId("settings-test-result")).toHaveTextContent("unreachable"));
  });
});

describe("planned pages render their layout on mock data, under the banner", () => {
  const cases: [string, React.ReactElement, string, string][] = [
    ["Strategies", <Strategies />, "strategy-row", "B11"],
    ["Backtests", <Backtests />, "backtest-row", "B11"],
    ["Bots", <Bots />, "bot-row", "B12"],
    ["Live log", <LiveLog />, "livelog-row", "B6"],
    ["Connections", <Connections />, "connection-row", "B12"],
    ["Chat", <Chat />, "chat-message", "B8"],
    ["Account", <Account />, "account-tokens", "B13"],
    ["Agent log", <AgentLog />, "agentlog-row", "B8"],
  ];
  for (const [label, el, rowId, stage] of cases) {
    it(`${label}`, () => {
      wrap(el);
      expect(screen.getByTestId("page-title")).toHaveTextContent(label);
      expect(screen.getByTestId("mock-banner")).toHaveAttribute("data-stage", stage);
      expect(screen.getAllByTestId(rowId).length).toBeGreaterThan(0);
      for (const b of screen.queryAllByTestId("planned-action")) expect(b).toBeDisabled();
    });
  }

  it("Live log filters by type", async () => {
    const user = userEvent.setup();
    wrap(<LiveLog />);
    expect(screen.getAllByTestId("livelog-row")).toHaveLength(6);
    await user.selectOptions(screen.getByTestId("filter-type"), "error");
    expect(screen.getAllByTestId("livelog-row")).toHaveLength(1);
    expect(screen.getByTestId("livelog-count")).toHaveTextContent("1 of 6");
  });
});
