import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { HealthV1 } from "@/lib/types";

export const HEALTH_POLL_MS = 15_000;

export interface HealthPoll {
  health: HealthV1 | null;
  /** Last error; cleared on the next successful poll. */
  error: Error | null;
  lastOkAt: Date | null;
}

/** Polls /api/v1/health every `pollMs` (15 s per ТЗ-B0 §5). Network errors
 *  never throw out of the hook: they land in `error`, the last good `health`
 *  stays. */
export function useHealth(pollMs: number = HEALTH_POLL_MS): HealthPoll {
  const [state, setState] = useState<HealthPoll>({ health: null, error: null, lastOkAt: null });

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const health = await api.health();
        if (alive) setState({ health, error: null, lastOkAt: new Date() });
      } catch (e) {
        if (alive) setState((s) => ({ ...s, error: e instanceof Error ? e : new Error(String(e)) }));
      }
      if (alive) timer = window.setTimeout(tick, pollMs);
    };
    void tick();
    return () => {
      alive = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [pollMs]);

  return state;
}
