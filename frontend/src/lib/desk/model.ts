// The Desk's pure logic, ported one to one from service/static/app.js so the
// React page draws exactly what the legacy page draws. No data processing
// beyond selecting, sorting and snapping what the service already computed.
import { fmtPrice } from "@/lib/desk/format";
import type { CandleRow } from "@/lib/types";

export type Tf = "1w" | "1d" | "4h" | "2h" | "1h" | "5m";
export const TFS: readonly Tf[] = ["1w", "1d", "4h", "2h", "1h", "5m"];
export const TF_RANK: Record<Tf, number> = { "5m": 0, "1h": 1, "2h": 2, "4h": 3, "1d": 4, "1w": 5 };
export const TF_MS: Record<Tf, number> = { "5m": 3e5, "1h": 36e5, "2h": 72e5, "4h": 144e5, "1d": 864e5, "1w": 6048e5 };
export const NEXT_UP: Record<Tf, Tf | null> = { "5m": "1h", "1h": "2h", "2h": "4h", "4h": "1d", "1d": "1w", "1w": null };
export const NEAR_PCT = 1.5;
export const DEFAULT_TF: Tf = "1h";
export const CANDLES_LIMIT = 500;

export function isTf(v: unknown): v is Tf {
  return typeof v === "string" && (TFS as readonly string[]).includes(v);
}

/** Chart palette (matches the legacy style.css). */
export const PALETTE = {
  up: "#26a69a",
  down: "#ef5350",
  support: "#199e70",
  resist: "#e66767",
  accent: "#3987e5",
  near: "#fab219",
  text: "#c3c2b7",
  muted: "#898781",
  grid: "#2c2c2a",
  surface: "#1a1a19",
} as const;

export interface Live {
  price: number | null;
  ts: string | null;
  ok: boolean;
}

export interface ZoneMember {
  tf: string;
  price: number;
  score: number;
  class: string;
  source_range_id: string;
}

export interface Zone {
  price: number;
  price_lo: number;
  price_hi: number;
  kind: "support" | "resistance";
  tfs: string[];
  top_tf: Tf;
  strength: "strong" | "weak";
  score: number;
  confluence: number;
  dist_pct_from_live?: number | null;
  last_event?: string | null;
  last_event_ts?: string | null;
  members?: ZoneMember[];
}

export interface Level {
  tf: Tf;
  price: number;
  kind: string;
  strength: string;
  dist_pct_from_live?: number | null;
  last_event?: string | null;
  last_event_ts?: string | null;
  source_range_id?: string;
}

export interface ActiveRange {
  low: [number, number];
  high: [number, number];
  [extra: string]: unknown;
}

export interface DeskEvent {
  tf: Tf;
  ts: string;
  type: string;
  level_price: number;
  source_range_id?: string;
  bar_close?: number;
}

/** state.json as the service writes it, plus the fields /api/state adds. */
export interface DeskState {
  pair: string;
  status?: string | null;
  tfs_ready?: string[];
  error_reason?: string | null;
  generated_at?: string | null;
  last_bar_ts?: string | null;
  price_last_close_1h?: number | null;
  trend?: Record<string, string>;
  regime?: Record<string, string>;
  active_ranges?: Partial<Record<Tf, ActiveRange | null>>;
  levels?: Level[];
  zones?: Zone[];
  recent_events_24h?: DeskEvent[];
  recent_events_7d?: DeskEvent[];
  live?: Live;
}

export interface Toggles {
  strong: boolean;
  weak: boolean;
  ranges: boolean;
  events: boolean;
  verboseEvents: boolean;
}
export const DEFAULT_TOGGLES: Toggles = { strong: true, weak: true, ranges: true, events: true, verboseEvents: false };

/** A chart bar; `time` is seconds (the library's UTCTimestamp). */
export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export type LineStyleName = "solid" | "dashed";

export interface PriceLineSpec {
  price: number;
  color: string;
  lineWidth: 1 | 2 | 3;
  lineStyle: LineStyleName;
  axisLabelVisible: boolean;
  title: string;
}

export interface ZoneDraw {
  zone: Zone;
  /** Band edges first (when the zone spans a band), the labeled centre LAST. */
  lines: PriceLineSpec[];
}

export interface MarkerSpec {
  time: number;
  position: "inBar" | "aboveBar" | "belowBar";
  shape: "circle" | "square" | "arrowUp";
  color: string;
}

export function mapCandles(rows: CandleRow[]): Bar[] {
  return rows.map(([ts, o, h, l, c]) => ({ time: ts / 1000, open: o, high: h, low: l, close: c }));
}

/** Greatest candle time <= t, or null. The candles themselves are the grid
 *  (epoch-modulo flooring is wrong for 1w: the epoch is a Thursday). */
export function snapToCandle(candles: readonly Bar[], tSec: number): number | null {
  if (!candles.length || tSec < candles[0].time) return null;
  let lo = 0;
  let hi = candles.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (candles[mid].time <= tSec) lo = mid;
    else hi = mid - 1;
  }
  return candles[lo].time;
}

/** Zones visible on `tf` with the current toggles, as price-line specs.
 *  A multi-price zone is a band: thin dashed edges + a labeled centre; a
 *  single-member zone is one line. TF visibility keys on top_tf. */
export function zoneDraws(state: DeskState | null, tf: Tf, toggles: Toggles): ZoneDraw[] {
  if (!state) return [];
  const rank = TF_RANK[tf];
  const out: ZoneDraw[] = [];
  for (const z of state.zones ?? []) {
    if ((TF_RANK[z.top_tf] ?? -1) < rank) continue;
    if (z.strength === "strong" && !toggles.strong) continue;
    if (z.strength === "weak" && !toggles.weak) continue;
    const isS = z.kind === "support";
    const color = isS ? PALETTE.support : PALETTE.resist;
    const strong = z.strength === "strong";
    let title = `${z.top_tf.toUpperCase()} · ${isS ? "S" : "R"}`;
    if (z.confluence > 1) title += ` ×${z.confluence}`;
    const lines: PriceLineSpec[] = [];
    if (z.price_hi > z.price_lo) {
      for (const edge of [z.price_lo, z.price_hi]) {
        lines.push({ price: edge, color, lineWidth: 1, lineStyle: "dashed", axisLabelVisible: false, title: "" });
      }
    }
    lines.push({
      price: z.price,
      color,
      lineWidth: strong ? 2 : 1,
      lineStyle: strong ? "solid" : "dashed",
      axisLabelVisible: true,
      title,
    });
    out.push({ zone: z, lines });
  }
  return out;
}

/** Active range bands: current TF + the next higher one. */
export function rangeLineSpecs(state: DeskState | null, tf: Tf, toggles: Toggles): PriceLineSpec[] {
  if (!state || !toggles.ranges) return [];
  const out: PriceLineSpec[] = [];
  const tfsToShow: Tf[] = [tf, NEXT_UP[tf]].filter((t): t is Tf => t !== null);
  for (const t of tfsToShow) {
    const r = state.active_ranges?.[t];
    if (!r) continue;
    const mk = (price: number, color: string, title: string): PriceLineSpec => ({
      price,
      color,
      lineWidth: 1,
      lineStyle: "dashed",
      axisLabelVisible: false,
      title,
    });
    out.push(mk(r.low[0], PALETTE.support, `${t.toUpperCase()} range low`));
    out.push(mk(r.low[1], PALETTE.support, ""));
    out.push(mk(r.high[0], PALETTE.resist, ""));
    out.push(mk(r.high[1], PALETTE.resist, `${t.toUpperCase()} range high`));
  }
  return out;
}

const MARK: Record<string, Omit<MarkerSpec, "time">> = {
  touched: { shape: "circle", color: PALETTE.muted, position: "inBar" },
  rejected: { shape: "circle", color: PALETTE.support, position: "belowBar" },
  broken: { shape: "square", color: PALETTE.resist, position: "aboveBar" },
  reclaimed: { shape: "arrowUp", color: PALETTE.accent, position: "belowBar" },
};

/** Event markers (last 7 days) snapped to the loaded candles, same TF
 *  visibility rule as zones; touches only with the verbose toggle. */
export function markerSpecs(state: DeskState | null, tf: Tf, toggles: Toggles, candles: readonly Bar[]): MarkerSpec[] {
  if (!state || !toggles.events || !candles.length) return [];
  const rank = TF_RANK[tf];
  const first = candles[0].time;
  const last = candles[candles.length - 1].time;
  const markers: MarkerSpec[] = [];
  for (const e of state.recent_events_7d ?? []) {
    if ((TF_RANK[e.tf] ?? -1) < rank) continue;
    const t = String(e.type).replace("historical_level_", "");
    if (t === "touched" && !toggles.verboseEvents) continue;
    const m = MARK[t];
    if (!m) continue;
    const barT = snapToCandle(candles, new Date(e.ts).getTime() / 1000);
    if (barT === null || barT < first || barT > last) continue;
    markers.push({ time: barT, ...m });
  }
  markers.sort((a, b) => a.time - b.time);
  return markers;
}

/** The forming bar approximated from the live price. The grid is anchored to
 *  the last real bar, not the epoch. */
export function liveBarUpdate(lastBar: Bar, px: number, tf: Tf, nowS: number): Bar {
  const stepS = TF_MS[tf] / 1000;
  const ahead = Math.max(0, Math.floor((nowS - lastBar.time) / stepS));
  if (ahead > 0) return { time: lastBar.time + ahead * stepS, open: px, high: px, low: px, close: px };
  return { ...lastBar, close: px, high: Math.max(lastBar.high, px), low: Math.min(lastBar.low, px) };
}

export interface LevelRow {
  zone: Zone;
  dist: number;
  near: boolean;
  nearest: boolean;
  priceTxt: string;
  distTxt: string;
  tfsTxt: string;
}

/** The "nearest levels" table: zones sorted by distance to the live price
 *  (or the last 1h close), at most 14 rows, near-band flags. */
export function levelsTableRows(state: DeskState | null): LevelRow[] {
  if (!state) return [];
  const live = state.live;
  const livePx = (live?.ok && live.price) || state.price_last_close_1h;
  if (!livePx) return [];
  const zones = (state.zones ?? []).slice().sort((a, b) => Math.abs(a.price - livePx) - Math.abs(b.price - livePx));
  const rows: LevelRow[] = zones.slice(0, 14).map((z) => {
    const dist = ((z.price - livePx) / livePx) * 100;
    const priceTxt =
      fmtPrice(z.price_lo) === fmtPrice(z.price_hi) ? fmtPrice(z.price) : `${fmtPrice(z.price_lo)}–${fmtPrice(z.price_hi)}`;
    return {
      zone: z,
      dist,
      near: Math.abs(dist) <= NEAR_PCT,
      nearest: false,
      priceTxt,
      distTxt: `${dist >= 0 ? "+" : ""}${dist.toFixed(2)}%`,
      tfsTxt: (z.tfs ?? []).map((t) => t.toUpperCase()).join("+"),
    };
  });
  if (rows.length && rows[0].near) rows[0].nearest = true;
  return rows;
}

export const TREND_ARROW: Record<string, string> = { up: "▲ up", down: "▼ down", neutral: "◆ neutral" };

export type HealthDotClass = "ok" | "warn" | "bad";

export interface HealthDot {
  cls: HealthDotClass;
  text: string;
  title: string;
  banner: { text: string; kind: "stale" | "info" } | null;
}

/** The header dot + stale banner, the legacy rule: < 5 min live, < 15 min
 *  lag, else stale; a worker still catching up after a restart is "запуск"
 *  with an info banner instead of the alarm. */
export function healthDot(
  pairAge: number | null | undefined,
  workerAlive: boolean,
  workerPhase: string | null,
  freshPairs: number,
  totalPairs: number,
  pairStale: boolean,
): HealthDot {
  const starting = workerAlive && workerPhase === "startup";
  let cls: HealthDotClass;
  let text: string;
  if (pairAge !== null && pairAge !== undefined && pairAge < 300) {
    cls = "ok";
    text = "● live";
  } else if (pairAge !== null && pairAge !== undefined && pairAge < 900) {
    cls = "warn";
    text = "● lag";
  } else {
    cls = "bad";
    text = "● stale";
  }
  if (starting && cls === "bad") {
    cls = "warn";
    text = "● запуск";
  }
  const title =
    pairAge !== null && pairAge !== undefined ? `последний 5m-реген: ${Math.round(pairAge / 60)} мин назад` : "нет heartbeat";
  let banner: HealthDot["banner"] = null;
  if (starting && pairStale) {
    banner = { text: `воркер догоняет данные после запуска: ${freshPairs}/${totalPairs} пар обновлено`, kind: "info" };
  } else if (cls === "bad") {
    banner = { text: "данные устарели — воркер не обновлял 5m дольше 15 минут", kind: "stale" };
  }
  return { cls, text, title, banner };
}
