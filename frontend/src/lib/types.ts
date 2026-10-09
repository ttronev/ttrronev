// Response shapes of /api/v1 (ТЗ-B0 §6; server side: service/api_v1.py).
// Every response carries `version` and `generated_at`. List endpoints are
// wrapped in an object so the envelope fields have somewhere to live.

export interface Envelope {
  version: string;
  generated_at: string;
}

export interface TfHealth {
  /** Newest regen stamp for this timeframe across ready pairs. */
  last_regen_ts: string | null;
  age_s: number | null;
  /** Age of the oldest stamp across ready pairs (the worst pair). */
  max_age_s: number | null;
  pairs_stamped: number;
}

export type HealthStatus = "ok" | "degraded" | "down";

export interface HealthV1 extends Envelope {
  status: HealthStatus;
  worker_alive: boolean;
  worker_phase: string | null;
  /** false = dev mode: TTRRONEV_API_KEY unset, no auth on /api/v1. */
  auth_enabled: boolean;
  tfs: Record<string, TfHealth>;
  pairs_ready: number;
  pairs_total: number;
  pairs_stale: string[];
  /** Ready pairs: seconds since their last 5m regen (null = never stamped). */
  pair_age_5m_s: Record<string, number | null>;
  cycle_5m_s: number | null;
  rss_mb: number | null;
  stale_after_s: number;
  warn_after_s: number;
}

export interface PairV1 {
  pair: string;
  status: string;
  tfs_ready: string[];
  added_ts: string | null;
  error_reason: string | null;
}

export interface PairsV1 extends Envelope {
  pairs: PairV1[];
}

export interface AddPairsV1 extends Envelope {
  queued: string[];
  rejected: { symbol: string; reason: string }[];
}

export interface RemovePairV1 extends Envelope {
  removed: string;
}

/** The legacy /api/state body, shape unchanged (typed in src/lib/desk/model.ts). */
export interface StateV1 extends Envelope {
  pair: string;
  state: Record<string, unknown>;
}

/** [t, o, h, l, c, v] */
export type CandleRow = [number, number, number, number, number, number];

export interface CandlesV1 extends Envelope {
  pair: string;
  tf: string;
  candles: CandleRow[];
  has_more: boolean;
}

export interface LogsTailV1 extends Envelope {
  lines: string[];
  file: string;
  size_bytes: number;
  truncated: boolean;
}

export interface VersionV1 extends Envelope {
  git_commit: string;
  built_at: string | null;
  app_version: string;
}
