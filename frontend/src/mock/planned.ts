// Mock data for the pages whose real data arrives in a later stage (ТЗ-B0 §5).
// Shapes follow the App structure's six objects: Strategy version, Backtest
// run, Scorecard, Connection, Bot, Journal. Deterministic.

export type Lifecycle = "draft" | "backtested" | "paper" | "live" | "retired";

export interface StrategyRow {
  id: string;
  name: string;
  lifecycle: Lifecycle;
  version: string;
  trades: number | null;
  winRate: number | null;
  evPerTrade: number | null;
  profitFactor: number | null;
  maxDrawdown: number | null;
  updated: string;
}

export const STRATEGIES: StrategyRow[] = [
  { id: "s1", name: "Secondary-only engine", lifecycle: "paper", version: "1.4.2", trades: 212, winRate: 0.47, evPerTrade: 0.31, profitFactor: 1.42, maxDrawdown: -6.8, updated: "2026-10-05" },
  { id: "s2", name: "Range-edge reclaim", lifecycle: "backtested", version: "0.3.0", trades: 88, winRate: 0.41, evPerTrade: 0.12, profitFactor: 1.08, maxDrawdown: -9.1, updated: "2026-10-02" },
  { id: "s3", name: "Zone confluence breakout", lifecycle: "draft", version: "0.1.0", trades: null, winRate: null, evPerTrade: null, profitFactor: null, maxDrawdown: null, updated: "2026-10-06" },
  { id: "s4", name: "Weekly level fade", lifecycle: "retired", version: "0.9.4", trades: 340, winRate: 0.38, evPerTrade: -0.04, profitFactor: 0.93, maxDrawdown: -14.2, updated: "2026-08-19" },
];

export interface BacktestRow {
  id: string;
  strategy: string;
  pairs: string;
  period: string;
  fees: string;
  status: "done" | "running" | "failed";
  result: string;
  configHash: string;
  commit: string;
  ran: string;
}

export const BACKTESTS: BacktestRow[] = [
  { id: "bt-0412", strategy: "Secondary-only engine @1.4.2", pairs: "SOL, BTC, ETH", period: "2024-01-01 → 2025-12-31", fees: "taker 0.05%", status: "done", result: "PF 1.42 · EV +0.31R · DD −6.8%", configHash: "9f2c1e…a1", commit: "5ee5a0a", ran: "2026-10-05 14:02" },
  { id: "bt-0411", strategy: "Range-edge reclaim @0.3.0", pairs: "SOL", period: "2024-06-01 → 2025-12-31", fees: "taker 0.05%", status: "done", result: "PF 1.08 · EV +0.12R · DD −9.1%", configHash: "41bb07…c9", commit: "5ee5a0a", ran: "2026-10-02 09:41" },
  { id: "bt-0410", strategy: "Weekly level fade @0.9.4", pairs: "19 pairs", period: "2023-01-01 → 2025-06-30", fees: "taker 0.05% + funding", status: "failed", result: "point-in-time guard tripped (future bar read)", configHash: "d0e5aa…7f", commit: "0bb0e09", ran: "2026-08-19 22:10" },
];

export interface BotRow {
  id: string;
  name: string;
  strategy: string;
  connection: string;
  mode: "paper" | "testnet" | "live";
  status: "running" | "paused" | "stopped";
  positions: number;
  pnl: string;
  since: string;
}

export const BOTS: BotRow[] = [
  { id: "b1", name: "sol-paper-1", strategy: "Secondary-only engine @1.4.2", connection: "paper (no exchange)", mode: "paper", status: "running", positions: 1, pnl: "+1.9R", since: "2026-10-01" },
  { id: "b2", name: "btc-testnet", strategy: "Secondary-only engine @1.4.2", connection: "Hyperliquid testnet", mode: "testnet", status: "paused", positions: 0, pnl: "+0.4R", since: "2026-10-04" },
];

export interface LiveLogRow {
  ts: string;
  bot: string;
  pair: string;
  type: "bias_flip" | "setup" | "entry" | "exit" | "error" | "agent";
  message: string;
}

export const LIVE_LOG: LiveLogRow[] = [
  { ts: "2026-10-06 11:55:14", bot: "sol-paper-1", pair: "SOL_USDT", type: "setup", message: "secondary pullback into 4H·S ×2 band 114.33–114.34" },
  { ts: "2026-10-06 11:40:02", bot: "sol-paper-1", pair: "SOL_USDT", type: "bias_flip", message: "1D bias up → up (ranging), 4H trending" },
  { ts: "2026-10-06 10:05:31", bot: "btc-testnet", pair: "BTC_USDT", type: "exit", message: "TP1 hit +1.0R, runner trailed" },
  { ts: "2026-10-06 08:00:09", bot: "btc-testnet", pair: "BTC_USDT", type: "entry", message: "long 0.05 @ 123,420 (SL 122,910)" },
  { ts: "2026-10-06 07:59:58", bot: "—", pair: "—", type: "agent", message: "researcher proposed challenger c-017 (ATR-normalised thresholds)" },
  { ts: "2026-10-05 23:11:40", bot: "btc-testnet", pair: "BTC_USDT", type: "error", message: "order rejected: insufficient testnet margin (retried)" },
];

export interface ConnectionRow {
  id: string;
  exchange: string;
  label: string;
  permissions: string;
  status: "ok" | "failed" | "untested";
  lastTest: string;
}

export const CONNECTIONS: ConnectionRow[] = [
  { id: "c1", exchange: "Hyperliquid", label: "agent wallet (trade-only)", permissions: "trade · never withdraw", status: "ok", lastTest: "2026-10-06 11:40" },
  { id: "c2", exchange: "Hyperliquid testnet", label: "testnet agent", permissions: "trade", status: "ok", lastTest: "2026-10-04 18:02" },
  { id: "c3", exchange: "CCXT · OKX", label: "read-only (later)", permissions: "read", status: "untested", lastTest: "—" },
];

export interface ChatMessage {
  role: "user" | "agent";
  ts: string;
  text: string;
}

export const CHAT: ChatMessage[] = [
  { role: "user", ts: "11:52", text: "Why is 114.33 marked strong? Price has not touched it since September." },
  { role: "agent", ts: "11:52", text: "It is a ×2 zone: a 4H level (score 0.84) and a 1H level (0.62) merged within 0.3 ATR. Strength comes from range cleanness and age; it has zero touches, which is the known gap in L5.1 (untested levels score strong). I can draft a challenger that discounts untested levels; it would run through the gate, not change anything live." },
  { role: "user", ts: "11:53", text: "Draft it into the Builder." },
  { role: "agent", ts: "11:53", text: "Drafted as strategy version 0.1.0 \"Zone confluence breakout\" with the untested-level discount as a parameter. Nothing is placed or promoted; the scorecard decides." },
];

export const ACCOUNT = {
  profile: { name: "Egor", role: "operator", since: "2026-04-29" },
  channels: [
    { type: "Telegram", target: "@ttrronev_alerts", status: "active", note: "structural alerts, worker errors" },
    { type: "Email", target: "—", status: "not set", note: "" },
  ],
  tokens: [
    { label: "laptop app", masked: "••••••••4f1c", created: "2026-10-06", lastUsed: "2026-10-06 11:55" },
    { label: "vps watchdog", masked: "••••••••90ab", created: "2026-10-06", lastUsed: "—" },
  ],
  llmUsage: { month: "2026-10", requests: 412, tokens: 1_240_000, cost: "$3.12" },
};

export interface AgentLogRow {
  ts: string;
  agent: "watchdog" | "ops triage" | "daily analyst" | "researcher" | "skeptic" | "reviewer";
  action: string;
  result: string;
  status: "proposed" | "ran" | "refused" | "vetoed";
}

export const AGENT_LOG: AgentLogRow[] = [
  { ts: "2026-10-06 07:59", agent: "researcher", action: "propose challenger c-017: ATR-normalised touch/break thresholds (9b)", result: "pre-registered; sandbox run queued for the owner", status: "proposed" },
  { ts: "2026-10-06 06:00", agent: "daily analyst", action: "digest 2026-10-05", result: "9 alerts, 6 reached the level, 2 reacted; 1 flicker (R141 1H)", status: "ran" },
  { ts: "2026-10-05 21:14", agent: "skeptic", action: "review claimed win c-015", result: "vetoed: validation window overlaps a regime change; n=41", status: "vetoed" },
  { ts: "2026-10-05 18:30", agent: "ops triage", action: "restart worker (whitelisted script)", result: "refused: health was green, no alarm from the watchdog", status: "refused" },
  { ts: "2026-10-05 18:25", agent: "watchdog", action: "health probe", result: "api up, worker alive, data fresh", status: "ran" },
];
