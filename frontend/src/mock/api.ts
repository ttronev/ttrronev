// Fixtures behind the mock flag (src/lib/api.ts MOCK). Shapes mirror
// src/lib/types.ts and src/lib/desk/model.ts. Deterministic: no clocks, no
// randomness, so the Playwright smoke test and screenshots are reproducible.
import { TF_MS, isTf, type DeskState, type Tf, type Zone } from "@/lib/desk/model";
import { BUNDLED_STAGES, type StagesFile } from "@/lib/stages";
import type {
  AddPairsV1,
  CandleRow,
  CandlesV1,
  HealthV1,
  LogsTailV1,
  PairsV1,
  RemovePairV1,
  StateV1,
  VersionV1,
} from "@/lib/types";

const GENERATED_AT = "2026-10-06T12:00:00Z";
const VERSION = "1";
/** Every mock series ends here; events sit a few bars before it. */
export const MOCK_END_MS = Date.UTC(2026, 9, 6, 12, 0, 0);
const DAY_MS = 864e5;

export const MOCK_PAIRS = ["SOL_USDT", "BTC_USDT", "ETH_USDT"] as const;

function tf(last: string, age: number) {
  return { last_regen_ts: last, age_s: age, max_age_s: age + 12, pairs_stamped: MOCK_PAIRS.length };
}

export async function health(): Promise<HealthV1> {
  return {
    version: VERSION,
    generated_at: GENERATED_AT,
    status: "ok",
    worker_alive: true,
    worker_phase: "running",
    auth_enabled: false,
    tfs: {
      "5m": tf("2026-10-06T11:55:12Z", 288),
      "1h": tf("2026-10-06T11:00:41Z", 3559),
      "2h": tf("2026-10-06T10:00:50Z", 7150),
      "4h": tf("2026-10-06T08:01:02Z", 14338),
      "1d": tf("2026-10-06T00:01:30Z", 43110),
      "1w": tf("2026-10-05T00:02:11Z", 129469),
    },
    pairs_ready: MOCK_PAIRS.length,
    pairs_total: MOCK_PAIRS.length,
    pairs_stale: [],
    pair_age_5m_s: Object.fromEntries(MOCK_PAIRS.map((p) => [p, 288])),
    cycle_5m_s: 4.2,
    rss_mb: 152,
    stale_after_s: 900,
    warn_after_s: 300,
  };
}

export async function pairs(): Promise<PairsV1> {
  return {
    version: VERSION,
    generated_at: GENERATED_AT,
    pairs: MOCK_PAIRS.map((pair) => ({
      pair,
      status: "ready",
      tfs_ready: ["1w", "1d", "4h", "2h", "1h", "5m"],
      added_ts: "2026-07-30T07:33:09+00:00",
      error_reason: null,
    })),
  };
}

export async function addPairs(symbols: string[]): Promise<AddPairsV1> {
  return {
    version: VERSION,
    generated_at: GENERATED_AT,
    queued: [],
    rejected: symbols.map((symbol) => ({ symbol, reason: "mock mode: the registry is read-only" })),
  };
}

export async function removePair(pair: string): Promise<RemovePairV1> {
  return { version: VERSION, generated_at: GENERATED_AT, removed: pair };
}

const iso = (ms: number) => new Date(ms).toISOString().replace(".000Z", "+00:00");

const ZONES: Zone[] = [
  {
    price: 98.5, price_lo: 98.4, price_hi: 98.6, kind: "support", tfs: ["4h", "1h"], top_tf: "4h",
    strength: "strong", score: 0.81, confluence: 2, dist_pct_from_live: -1.5,
    last_event: "rejected", last_event_ts: iso(MOCK_END_MS - 2 * DAY_MS),
    members: [
      { tf: "4h", price: 98.6, score: 0.84, class: "strong", source_range_id: "R12_4h_up_20260920" },
      { tf: "1h", price: 98.4, score: 0.62, class: "weak", source_range_id: "R140_1h_up_20260930" },
    ],
  },
  {
    price: 101.2, price_lo: 101.2, price_hi: 101.2, kind: "resistance", tfs: ["1h"], top_tf: "1h",
    strength: "weak", score: 0.42, confluence: 1, dist_pct_from_live: 1.2,
    last_event: "broken", last_event_ts: iso(MOCK_END_MS - DAY_MS),
    members: [{ tf: "1h", price: 101.2, score: 0.42, class: "weak", source_range_id: "R141_1h_down_20261001" }],
  },
  {
    price: 95.0, price_lo: 95.0, price_hi: 95.0, kind: "support", tfs: ["1d"], top_tf: "1d",
    strength: "strong", score: 0.9, confluence: 1, dist_pct_from_live: -5,
    last_event: "touched", last_event_ts: iso(MOCK_END_MS - 3 * DAY_MS),
    members: [{ tf: "1d", price: 95.0, score: 0.9, class: "strong", source_range_id: "R38_1d_up_20260801" }],
  },
  {
    price: 106.0, price_lo: 106.0, price_hi: 106.0, kind: "resistance", tfs: ["4h"], top_tf: "4h",
    strength: "strong", score: 0.77, confluence: 1, dist_pct_from_live: 6,
    last_event: null, last_event_ts: null,
    members: [{ tf: "4h", price: 106.0, score: 0.77, class: "strong", source_range_id: "R13_4h_down_20260925" }],
  },
];

export function mockState(pair: string): DeskState {
  return {
    pair,
    status: "ready",
    tfs_ready: ["1w", "1d", "4h", "2h", "1h", "5m"],
    error_reason: null,
    generated_at: GENERATED_AT,
    last_bar_ts: iso(MOCK_END_MS - 36e5),
    price_last_close_1h: 100.2,
    trend: { "1d": "up", "4h": "neutral", basis: "structure+ema" },
    regime: { "1d": "ranging", "4h": "trending" },
    active_ranges: {
      "1w": null,
      "1d": null,
      "4h": { low: [94.5, 95.5], high: [105.5, 106.5] },
      "2h": null,
      "1h": { low: [97.8, 98.6], high: [101.0, 101.6] },
      "5m": null,
    },
    levels: ZONES.flatMap((z) =>
      (z.members ?? []).map((m) => ({
        tf: m.tf as Tf,
        price: m.price,
        kind: z.kind,
        strength: m.class,
        dist_pct_from_live: z.dist_pct_from_live,
        last_event: z.last_event,
        last_event_ts: z.last_event_ts,
        source_range_id: m.source_range_id,
      })),
    ),
    zones: ZONES,
    recent_events_24h: [],
    recent_events_7d: [
      { tf: "1h", ts: iso(MOCK_END_MS - 2 * DAY_MS), type: "rejected", level_price: 98.5, source_range_id: "R140_1h_up_20260930", bar_close: 98.9 },
      { tf: "1h", ts: iso(MOCK_END_MS - DAY_MS), type: "broken", level_price: 101.2, source_range_id: "R141_1h_down_20261001", bar_close: 102.1 },
      { tf: "1d", ts: iso(MOCK_END_MS - 3 * DAY_MS), type: "historical_level_touched", level_price: 95.0, source_range_id: "R38_1d_up_20260801", bar_close: 95.4 },
    ],
    live: { price: 100.0, ts: GENERATED_AT, ok: true },
  };
}

export async function state(pair: string): Promise<StateV1> {
  return { version: VERSION, generated_at: GENERATED_AT, pair, state: mockState(pair) as unknown as Record<string, unknown> };
}

/** A smooth, obviously synthetic series around 100 for any timeframe:
 *  `n` bars ending at MOCK_END_MS (or before `before`), strictly increasing. */
export function mockCandles(tfName: string, limit: number, before?: number): CandleRow[] {
  const step = isTf(tfName) ? TF_MS[tfName] : TF_MS["5m"];
  const n = Math.max(0, Math.min(limit, 300));
  const end = before !== undefined ? Math.floor(before / step) * step : MOCK_END_MS;
  const rows: CandleRow[] = [];
  let prevClose = 100;
  for (let i = 0; i < n; i++) {
    const t = end - (n - i) * step;
    const k = t / step; // index on the bar grid: continuous across pages
    const close = 100 + 3 * Math.sin(k / 9) + 0.8 * Math.cos(k / 3.7);
    const open = prevClose;
    const high = Math.max(open, close) + 0.4;
    const low = Math.min(open, close) - 0.4;
    rows.push([t, round2(open), round2(high), round2(low), round2(close), 1000 + (i % 7) * 100]);
    prevClose = close;
  }
  return rows;
}

const round2 = (x: number) => Math.round(x * 100) / 100;

export async function candles(pair: string, tfName: string, limit: number, before?: number): Promise<CandlesV1> {
  const rows = mockCandles(tfName, limit, before);
  // One page of older history is available; after that the mock is exhausted.
  return { version: VERSION, generated_at: GENERATED_AT, pair, tf: tfName, candles: rows, has_more: before === undefined };
}

export async function logsTail(lines: number): Promise<LogsTailV1> {
  const all = [
    "2026-10-06 11:50:03 [worker] heartbeat started",
    "2026-10-06 11:50:09 [worker] SOL_USDT: startup complete — artifacts current, nothing regenerated",
    "2026-10-06 11:50:11 [worker] BTC_USDT: startup complete — artifacts current, nothing regenerated",
    "2026-10-06 11:50:12 [worker] ETH_USDT: startup complete — artifacts current, nothing regenerated",
    "2026-10-06 11:55:12 [worker] 5m pass: total 4.2s over 3 pair(s), peak RSS 152 MB (SOL_USDT: 1.3s, BTC_USDT: 1.6s, ETH_USDT: 1.3s)",
  ];
  const keep = Math.max(0, Math.min(lines, all.length));
  const out = keep === 0 ? [] : all.slice(-keep);
  return {
    version: VERSION,
    generated_at: GENERATED_AT,
    lines: out,
    file: "worker.log",
    size_bytes: 4096,
    truncated: keep < all.length,
  };
}

export async function stages(): Promise<StagesFile> {
  return BUNDLED_STAGES;
}

export async function version(): Promise<VersionV1> {
  return {
    version: VERSION,
    generated_at: GENERATED_AT,
    git_commit: "mock0000",
    built_at: GENERATED_AT,
    app_version: "0.1.0",
  };
}
