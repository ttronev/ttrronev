// Candle cache + paging, ported from the legacy page: instant TF switches
// from cache, the fresh tail spliced onto already-paged older history, one
// in-flight "older" request per view.
import { api } from "@/lib/api";
import { CANDLES_LIMIT, mapCandles, type Bar } from "@/lib/desk/model";

export interface CacheEntry {
  candles: Bar[];
  fetchedAt: number;
  hasMore: boolean;
  loadingOlder: boolean;
}

export class CandleCache {
  private readonly map = new Map<string, CacheEntry>();

  static key(pair: string, tf: string): string {
    return `${pair}:${tf}`;
  }

  get(pair: string, tf: string): CacheEntry | undefined {
    return this.map.get(CandleCache.key(pair, tf));
  }

  delete(pair: string, tf: string): void {
    this.map.delete(CandleCache.key(pair, tf));
  }

  /** The view's candles: from cache unless `force`; a refresh keeps the
   *  older history already paged in and splices the fresh tail onto it. */
  async load(pair: string, tf: string, opts: { force?: boolean; signal?: AbortSignal } = {}): Promise<Bar[]> {
    const key = CandleCache.key(pair, tf);
    const hit = this.map.get(key);
    if (hit && !opts.force) return hit.candles;
    const j = await api.candles(pair, tf, CANDLES_LIMIT, undefined, opts.signal);
    const mapped = mapCandles(j.candles);
    if (hit && hit.candles.length) {
      const cut = mapped.length ? mapped[0].time : Infinity;
      const older = hit.candles.filter((b) => b.time < cut);
      this.map.set(key, { candles: older.concat(mapped), fetchedAt: Date.now(), hasMore: hit.hasMore, loadingOlder: false });
    } else {
      this.map.set(key, { candles: mapped, fetchedAt: Date.now(), hasMore: !!j.has_more, loadingOlder: false });
    }
    return this.map.get(key)!.candles;
  }

  /** Page older candles in (infinite left scroll). Resolves to the grown
   *  array, or null when nothing was added. */
  async loadOlder(pair: string, tf: string): Promise<Bar[] | null> {
    const c = this.get(pair, tf);
    if (!c || !c.hasMore || c.loadingOlder || !c.candles.length) return null;
    c.loadingOlder = true;
    try {
      const before = Math.round(c.candles[0].time * 1000);
      let j;
      try {
        j = await api.candles(pair, tf, CANDLES_LIMIT, before);
      } catch {
        c.hasMore = false;
        return null;
      }
      const older = mapCandles(j.candles).filter((b) => b.time < c.candles[0].time);
      c.hasMore = !!j.has_more && older.length > 0;
      if (!older.length) return null;
      c.candles = older.concat(c.candles);
      return c.candles;
    } finally {
      c.loadingOlder = false;
    }
  }
}
