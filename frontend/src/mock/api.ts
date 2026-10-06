// Fixtures behind the mock flag (src/lib/api.ts MOCK). Shapes mirror
// src/lib/types.ts. Deterministic: no clocks, no randomness, so the Playwright
// smoke test and screenshots are reproducible.
import { BUNDLED_STAGES, type StagesFile } from "@/lib/stages";
import type { CandleRow, CandlesV1, HealthV1, LogsTailV1, PairsV1, StateV1, VersionV1 } from "@/lib/types";

const GENERATED_AT = "2026-10-06T12:00:00Z";
const VERSION = "1";

export const MOCK_PAIRS = ["SOL/USDT", "BTC/USDT", "ETH/USDT"] as const;

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

export async function state(pair: string): Promise<StateV1> {
  return {
    version: VERSION,
    generated_at: GENERATED_AT,
    pair,
    state: { pair, generated_at: GENERATED_AT, levels: [], zones: [], live: { price: 100, ts: GENERATED_AT, ok: true } },
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
  return { version: VERSION, generated_at: GENERATED_AT, pair, tf, candles: rows, has_more: false };
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
