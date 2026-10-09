// The sidebar, as data. Order and grouping are a product contract (ТЗ-B0 §5):
// eight user-facing sections, then the Admin group (operator only) last.
// Pages that are not live in B0 carry the stage that delivers their data; the
// shell shows the "Mock data — real data arrives in stage Bx" banner for them.

export type NavGroup = "main" | "admin";

export interface NavEntry {
  /** Stable id (also the data-nav-id on the sidebar link). */
  id: string;
  label: string;
  /** Route path relative to the router basename (/app). */
  path: string;
  group: NavGroup;
  /** Live in B0: reads real data through /api/v1. */
  live: boolean;
  /** For non-live pages: the stage (docs/plan/stages.json id) that brings real data. */
  stage?: string;
  /** One line from the App structure: what the page is for. */
  blurb: string;
}

export const ADMIN_GROUP_LABEL = "Admin";

export const NAV: readonly NavEntry[] = [
  {
    id: "desk",
    label: "Desk",
    path: "/",
    group: "main",
    live: true,
    blurb: "The market: chart, zones, volume profile, candidate cards for the pairs you follow.",
  },
  {
    id: "strategies",
    label: "Strategies",
    path: "/strategies",
    group: "main",
    live: false,
    stage: "B11",
    blurb: "Your strategies with lifecycle state and headline numbers; versions, backtests, bots using them.",
  },
  {
    id: "backtests",
    label: "Backtests",
    path: "/backtests",
    group: "main",
    live: false,
    stage: "B11",
    blurb: "Pick a strategy version, pairs, period and fee model; run; results land on the scorecard.",
  },
  {
    id: "bots",
    label: "Bots",
    path: "/bots",
    group: "main",
    live: false,
    stage: "B12",
    blurb: "One strategy version + one connection + risk limits, in paper, testnet or live mode.",
  },
  {
    id: "live-log",
    label: "Live log",
    path: "/live-log",
    group: "main",
    live: false,
    stage: "B6",
    blurb: "One event stream across all bots: bias flips, setups, entries, exits, errors, agent actions.",
  },
  {
    id: "connections",
    label: "Connections",
    path: "/connections",
    group: "main",
    live: false,
    stage: "B12",
    blurb: "Exchange accounts: test connection; keys encrypted at rest and never displayed again.",
  },
  {
    id: "chat",
    label: "Chat",
    path: "/chat",
    group: "main",
    live: false,
    stage: "B8",
    blurb: "The agent, aware of the screen you are on. It drafts and explains; it cannot edit numbers or place orders.",
  },
  {
    id: "account",
    label: "Account",
    path: "/account",
    group: "main",
    live: false,
    stage: "B13",
    blurb: "Profile, notification channels (Telegram), app tokens, LLM usage, data export.",
  },
  {
    id: "health",
    label: "Health",
    path: "/admin/health",
    group: "admin",
    live: true,
    blurb: "Worker heartbeat per timeframe, pairs and bootstrap state, cycle time, memory, API latency.",
  },
  {
    id: "logs",
    label: "Logs",
    path: "/admin/logs",
    group: "admin",
    live: true,
    blurb: "Tail of the worker log with auto-follow and a text filter.",
  },
  {
    id: "build",
    label: "Build",
    path: "/admin/build",
    group: "admin",
    live: true,
    blurb: "The stage map from docs/plan/stages.json and what is in progress now.",
  },
  {
    id: "agent-log",
    label: "Agent log",
    path: "/admin/agent-log",
    group: "admin",
    live: false,
    stage: "B8",
    blurb: "What the agents proposed, ran and were refused.",
  },
  {
    id: "settings",
    label: "Settings",
    path: "/admin/settings",
    group: "admin",
    live: true,
    blurb: "Service URL, API token (stored locally, masked), test connection; theme follows the system.",
  },
];

export const MAIN_ENTRIES = NAV.filter((e) => e.group === "main");
export const ADMIN_ENTRIES = NAV.filter((e) => e.group === "admin");

export function navByPath(pathname: string): NavEntry | undefined {
  const clean = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  return NAV.find((e) => e.path === clean);
}

export function navById(id: string): NavEntry {
  const entry = NAV.find((e) => e.id === id);
  if (!entry) throw new Error(`unknown nav entry: ${id}`);
  return entry;
}
