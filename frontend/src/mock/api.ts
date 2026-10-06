// Fixtures behind the mock flag (src/lib/api.ts MOCK). Shapes mirror
// src/lib/types.ts. Deterministic: no clocks, no randomness, so the Playwright
// smoke test and screenshots are reproducible.
import { BUNDLED_STAGES, type StagesFile } from "@/lib/stages";
import type { CandleRow, CandlesV1, HealthV1, LogsTailV1, PairsV1, StateV1, VersionV1 } from "@/lib/types";

const GENERATED_AT = "2026-10-06T12:00:00Z";
const VERSION = "1";

export const MOCK_PAIRS = ["SOL/USDT", "BTC/USDT", "ETH/USDT"] as const;

export async function health(): Promise<HealthV1> {
  return {
    version: VERSION,
    generated_at: GENERATED_AT,
    status: "ok",
    worker_alive: true,
    tfs: {
      "5m": { last_regen_ts: "2026-10-06T11:55:12Z", age_s: 288 },
      "1h": { last_regen_ts: "2026-10-06T11:00:41Z", age_s: 3559 },
      "2h": { last_regen_ts: "2026-10-06T10:00:50Z", age_s: 7150 },
      "4h": { last_regen_ts: "2026-10-06T08:01:02Z", age_s: 14338 },
      "1d": { last_regen_ts: "2026-10-06T00:01:30Z", age_s: 43110 },
      "1w": { last_regen_ts: "2026-10-05T00:02:11Z", age_s: 129469 },
    },
    pairs_ready: MOCK_PAIRS.length,
    cycle_5m_s: 4.2,
    rss_mb: 152,
  };
}

export async function pairs(): Promise<PairsV1> {
  return {
    version: VERSION,
    generated_at: GENERATED_AT,
    pairs: MOCK_PAIRS.map((pair) => ({ pair, status: "ready", tfs_ready: ["1w", "1d", "4h", "2h", "1h", "5m"] })),
  };
}

export async function state(pair: string): Promise<StateV1> {
  return {
    version: VERSION,
    generated_at: GENERATED_AT,
    state: { pair, generated_at: GENERATED_AT, levels: [], zones: [] },
  };
}

export async function candles(pair: string, tf: string, limit: number): Promise<CandlesV1> {
  // A flat, obviously synthetic series: n bars at 100.0 with a 1% box.
  const n = Math.max(0, Math.min(limit, 100));
  const stepMs = 5 * 60 * 1000;
  const t0 = Date.UTC(2026, 9, 6, 0, 0, 0) - n * stepMs;
  const rows: CandleRow[] = [];
  for (let i = 0; i < n; i++) {
    rows.push([t0 + i * stepMs, 100, 101, 99, 100, 1000]);
  }
  return { version: VERSION, generated_at: GENERATED_AT, pair, tf, candles: rows };
}

export async function logsTail(lines: number): Promise<LogsTailV1> {
  const all = [
    "[worker] heartbeat started",
    "[SOL/USDT] startup complete — artifacts current, nothing regenerated",
    "[BTC/USDT] startup complete — artifacts current, nothing regenerated",
    "[ETH/USDT] startup complete — artifacts current, nothing regenerated",
    "[5m] cycle 11:55 — 3 ok, 0 failed, 4.2 s, peak RSS 152 MB",
  ];
  const keep = Math.max(0, Math.min(lines, all.length));
  const out = keep === 0 ? [] : all.slice(-keep);
  return { version: VERSION, generated_at: GENERATED_AT, lines: out, file: "worker.log", size_bytes: 4096 };
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
    app_version: "0.0.1",
  };
}
