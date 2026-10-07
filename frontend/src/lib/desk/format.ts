// Display formatting ported from service/static/app.js (the legacy page).
// priceDecimals mirrors display_decimals() in shared/pricefmt.py: keep the
// two in step.

export function priceDecimals(x: number): number {
  const ax = Math.abs(x);
  if (ax >= 1000) return 1;
  if (ax >= 10) return 2;
  if (ax >= 0.1) return 4;
  if (!ax || !Number.isFinite(ax)) return 2;
  return Math.max(6, Math.min(14, 3 - Math.floor(Math.log10(ax))));
}

export function fmtPrice(x: number | null | undefined): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const d = priceDecimals(x);
  return "$" + x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

/** "2026-10-06 19:35 UTC" from an ISO string. */
export function fmtTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}

/** "2026-10-06 19:35:12 UTC" (seconds precision, the live-price contract). */
export function fmtTsSeconds(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().replace("T", " ").slice(0, 19) + " UTC";
}

export function ageStr(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (!iso) return "";
  const s = Math.max(0, (nowMs - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
}

/** "SOL_USDT" -> "SOL/USDT" */
export function pairLabel(pair: string): string {
  return pair.replace("_", "/");
}
