import { useCallback, useEffect, useRef, useState } from "react";

export interface Polled<T> {
  data: T | null;
  /** Last error; cleared by the next successful poll. */
  error: Error | null;
  lastOkAt: Date | null;
  loading: boolean;
  refresh: () => void;
}

/** Poll `fetcher` every `intervalMs`. Network errors never throw out of the
 *  hook: they land in `error` while the last good `data` stays, which is
 *  what the offline rule needs ("service unreachable" + last fetch time,
 *  keep retrying). A change in `deps` restarts the poll and clears `data`;
 *  `refresh()` restarts it and keeps the data. */
export function usePolled<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
  deps: readonly unknown[] = [],
  opts: { enabled?: boolean } = {},
): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [lastOkAt, setLastOkAt] = useState<Date | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const depsKey = JSON.stringify(deps);
  const lastDepsKey = useRef(depsKey);
  const enabled = opts.enabled !== false;

  useEffect(() => {
    if (lastDepsKey.current !== depsKey) {
      lastDepsKey.current = depsKey;
      setData(null);
      setError(null);
    }
    if (!enabled) {
      setLoading(false);
      return;
    }
    let alive = true;
    let timer: number | undefined;
    const ac = new AbortController();
    setLoading(true);
    const tick = async () => {
      try {
        const value = await fetcherRef.current(ac.signal);
        if (!alive) return;
        setData(value);
        setError(null);
        setLastOkAt(new Date());
      } catch (e) {
        if (!alive || (e as { name?: string }).name === "AbortError") return;
        setError(e instanceof Error ? e : new Error(String(e)));
      } finally {
        if (alive) setLoading(false);
      }
      if (alive) timer = window.setTimeout(tick, intervalMs);
    };
    void tick();
    return () => {
      alive = false;
      ac.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [intervalMs, nonce, enabled, depsKey]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, lastOkAt, loading, refresh };
}
