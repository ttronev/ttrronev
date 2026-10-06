// Response shapes of /api/v1 (ТЗ-B0 §6). Every response carries `version`
// and `generated_at`. List endpoints are wrapped in an object so the envelope
// fields have somewhere to live (to confirm at the Layer 2 lock).

export interface Envelope {
  version: string;
  generated_at: string;
}

export interface TfHealth {
  last_regen_ts: string | null;
  age_s: number | null;
}

export interface HealthV1 extends Envelope {
  status: "ok" | "degraded" | "down";
  worker_alive: boolean;
  tfs: Record<string, TfHealth>;
  pairs_ready: number;
  cycle_5m_s: number | null;
  rss_mb: number | null;
}

export interface PairV1 {
  pair: string;
  status: string;
  tfs_ready: string[];
}

export interface PairsV1 extends Envelope {
  pairs: PairV1[];
}

/** state.json as today, unchanged shape (typed loosely until the Desk port). */
export interface StateV1 extends Envelope {
  state: Record<string, unknown>;
}

/** [t, o, h, l, c, v] */
export type CandleRow = [number, number, number, number, number, number];

export interface CandlesV1 extends Envelope {
  pair: string;
  tf: string;
  candles: CandleRow[];
}

export interface LogsTailV1 extends Envelope {
  lines: string[];
  file: string;
  size_bytes: number;
}

export interface VersionV1 extends Envelope {
  git_commit: string;
  built_at: string;
  app_version: string;
}
