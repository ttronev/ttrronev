// Desk: the legacy dashboard ported as a React page. Same data, same
// behaviour, same labels; the chart is TradingView Lightweight Charts 4.2.0
// unchanged. Polling only: state 15 s, candles 60 s, registry 12 s,
// bootstrap progress 3 s, health 15 s (useHealth).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DeskChart } from "@/components/desk/Chart";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { CandleCache } from "@/lib/desk/candles";
import { ageStr, fmtPrice, fmtTs, fmtTsSeconds, pairLabel } from "@/lib/desk/format";
import {
  DEFAULT_TF,
  DEFAULT_TOGGLES,
  TFS,
  TREND_ARROW,
  healthDot,
  isTf,
  levelsTableRows,
  markerSpecs,
  rangeLineSpecs,
  zoneDraws,
  type Bar,
  type DeskState,
  type HealthDot,
  type Tf,
  type Toggles,
} from "@/lib/desk/model";
import type { PairV1 } from "@/lib/types";
import { useHealth } from "@/lib/useHealth";
import { usePolled } from "@/lib/usePolled";
import { cn } from "@/lib/utils";

const POLL_STATE_MS = 15_000;
const POLL_CANDLES_MS = 60_000;
const POLL_BOOTSTRAP_MS = 3_000;
const POLL_REGISTRY_MS = 12_000;
const LS_TF = "ttr_tf";
const LS_TOGGLES = "ttr_toggles";

declare global {
  interface Window {
    /** Acceptance hooks (same as the legacy page): what the chart draws. */
    __zonesRendered?: { price: number; lo: number; hi: number; n: number }[];
    __levelsRendered?: number[];
  }
}

function loadTf(): Tf {
  try {
    const v = window.localStorage.getItem(LS_TF);
    return isTf(v) ? v : DEFAULT_TF;
  } catch {
    return DEFAULT_TF;
  }
}

function loadToggles(): Toggles {
  try {
    return { ...DEFAULT_TOGGLES, ...(JSON.parse(window.localStorage.getItem(LS_TOGGLES) || "{}") as Partial<Toggles>) };
  } catch {
    return DEFAULT_TOGGLES;
  }
}

function statusMark(e: PairV1): string {
  return e.status === "ready" ? "●" : e.status === "bootstrapping" ? "◌" : "✕";
}

function rejectedText(d: unknown): string | null {
  if (typeof d === "string") return d;
  if (d && typeof d === "object" && Array.isArray((d as { rejected?: unknown }).rejected)) {
    return (d as { rejected: { symbol: string; reason: string }[] }).rejected.map((x) => `${x.symbol}: ${x.reason}`).join("; ");
  }
  return null;
}

const DOT_CLASS: Record<HealthDot["cls"], string> = {
  ok: "text-status-ok",
  warn: "text-status-warn",
  bad: "font-bold text-status-bad",
};

const TOGGLE_LABELS: { key: keyof Toggles; label: string; title?: string; testId: string }[] = [
  { key: "strong", label: "сильные", testId: "tg-strong" },
  { key: "weak", label: "слабые", testId: "tg-weak" },
  { key: "ranges", label: "диапазоны", testId: "tg-ranges" },
  { key: "events", label: "события", testId: "tg-events" },
  { key: "verboseEvents", label: "касания", title: "показывать касания уровней", testId: "tg-verbose" },
];

export default function Desk() {
  const [pair, setPair] = useState<string | null>(null);
  const [tf, setTf] = useState<Tf>(loadTf);
  const [toggles, setToggles] = useState<Toggles>(loadToggles);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  // Registry.
  const pairsPoll = usePolled((signal) => api.pairs(signal).then((r) => r.pairs), POLL_REGISTRY_MS);
  const entries = useMemo(() => pairsPoll.data ?? [], [pairsPoll.data]);
  useEffect(() => {
    if (pair === null && entries.length) {
      const ready = entries.find((e) => e.status === "ready");
      setPair((ready ?? entries[0]).pair);
    }
  }, [entries, pair]);

  // State of the selected pair.
  const statePoll = usePolled(
    (signal) => (pair ? api.state(pair, signal).then((r) => r.state as unknown as DeskState) : Promise.resolve(null)),
    POLL_STATE_MS,
    [pair],
    { enabled: pair !== null },
  );
  const state = statePoll.data && statePoll.data.pair === pair ? statePoll.data : null;

  // Candles: one view = pair + tf; cached per view, paged on left scroll.
  const cacheRef = useRef(new CandleCache());
  const [view, setView] = useState<{ key: string; candles: Bar[] }>({ key: "", candles: [] });
  const viewGen = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const loadView = useCallback(async (p: string, t: Tf, force: boolean) => {
    const gen = ++viewGen.current;
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    const key = CandleCache.key(p, t);
    try {
      const candles = await cacheRef.current.load(p, t, { force, signal: ac.signal });
      if (gen !== viewGen.current) return;
      setView({ key, candles: candles.slice() });
    } catch (e) {
      if ((e as { name?: string }).name === "AbortError" || gen !== viewGen.current) return;
      setView({ key, candles: [] });
    }
  }, []);
  useEffect(() => {
    if (!pair) return;
    void loadView(pair, tf, false);
  }, [pair, tf, loadView]);
  useEffect(() => {
    if (!pair) return;
    const id = window.setInterval(() => void loadView(pair, tf, true), POLL_CANDLES_MS);
    return () => window.clearInterval(id);
  }, [pair, tf, loadView]);
  const onNeedOlder = useCallback(() => {
    if (!pair) return;
    const p = pair;
    const t = tf;
    const gen = viewGen.current;
    void cacheRef.current.loadOlder(p, t).then((grown) => {
      if (grown && gen === viewGen.current) setView({ key: CandleCache.key(p, t), candles: grown.slice() });
    });
  }, [pair, tf]);

  // Overlays (pure selections from the state).
  const zones = useMemo(() => zoneDraws(state, tf, toggles), [state, tf, toggles]);
  const rangeLines = useMemo(() => rangeLineSpecs(state, tf, toggles), [state, tf, toggles]);
  const markers = useMemo(() => markerSpecs(state, tf, toggles, view.candles), [state, tf, toggles, view.candles]);
  useEffect(() => {
    window.__zonesRendered = zones.map((z) => ({ price: z.zone.price, lo: z.zone.price_lo, hi: z.zone.price_hi, n: z.zone.confluence }));
    window.__levelsRendered = zones.map((z) => z.zone.price);
  }, [zones]);
  const [flash, setFlash] = useState<{ price: number; nonce: number } | null>(null);

  // Health dot + banner.
  const { health, error: healthError } = useHealth();
  const dot = useMemo<HealthDot | null>(() => {
    if (healthError) {
      return {
        cls: "bad",
        text: "● api down",
        title: healthError.message,
        banner: { text: "данные устарели — воркер не обновлял 5m дольше 15 минут", kind: "stale" },
      };
    }
    if (!health || !pair) return null;
    const fresh = health.pairs_ready - health.pairs_stale.length;
    return healthDot(
      health.pair_age_5m_s[pair],
      health.worker_alive,
      health.worker_phase,
      fresh,
      health.pairs_ready,
      health.pairs_stale.includes(pair),
    );
  }, [health, healthError, pair]);

  // Bootstrap progress: the oldest bootstrapping pair, polled every 3 s.
  const boot = useMemo(
    () =>
      entries
        .filter((e) => e.status === "bootstrapping")
        .sort((a, b) => (a.added_ts ?? "").localeCompare(b.added_ts ?? "")),
    [entries],
  );
  const active = boot[0] ?? null;
  const bootPoll = usePolled(
    (signal) => (active ? api.state(active.pair, signal).then((r) => r.state as unknown as DeskState) : Promise.resolve(null)),
    POLL_BOOTSTRAP_MS,
    [active?.pair ?? null],
    { enabled: active !== null },
  );
  const bootState = bootPoll.data;
  const pendingAuto = useRef<string | null>(null);
  const refreshPairs = pairsPoll.refresh;
  useEffect(() => {
    if (!active || !bootState) return;
    if (bootState.status === "ready" || bootState.status === "error") refreshPairs();
  }, [bootState, active, refreshPairs]);
  useEffect(() => {
    // Switch only to the pair THIS user added, once it is ready; never yank
    // the view away from a pair they navigated to meanwhile.
    const target = pendingAuto.current;
    if (!target) return;
    const e = entries.find((x) => x.pair === target);
    if (e && e.status === "ready") {
      pendingAuto.current = null;
      cacheRef.current.delete(target, tf);
      setPair(target);
    }
  }, [entries, tf]);
  const bootTfs = (bootState?.tfs_ready ?? active?.tfs_ready ?? []).length;
  const bootText = active
    ? bootState?.status === "error"
      ? `ошибка загрузки ${pairLabel(active.pair)}: ${bootState.error_reason || "unknown"} (повторите добавление)`
      : `загрузка ${pairLabel(active.pair)}: ${bootTfs}/6 TF` +
        (boot.length > 1 ? ` · в очереди: ${boot.slice(1).map((e) => pairLabel(e.pair)).join(", ")}` : "")
    : null;
  const bootPct = active && bootState?.status !== "error" ? Math.round((bootTfs / 6) * 100) : 0;

  // Add pair.
  const [adding, setAdding] = useState(false);
  const [addText, setAddText] = useState("");
  const [addErr, setAddErr] = useState("");
  const submitAdd = async () => {
    const symbols = addText
      .split(/[,\n;]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (!symbols.length) return;
    setAddErr("");
    try {
      const r = await api.addPairs(symbols);
      if (r.rejected.length) setAddErr("отклонено: " + r.rejected.map((x) => `${x.symbol} (${x.reason})`).join("; "));
      pendingAuto.current = r.queued.length === 1 ? r.queued[0] : null;
      refreshPairs();
      if (r.queued.length) {
        setAdding(false);
        setAddText("");
      }
    } catch (e) {
      if (e instanceof ApiError) setAddErr(rejectedText(e.detail) ?? `HTTP ${e.status}`);
      else setAddErr(`сеть: ${(e as Error).message}`);
    }
  };

  const changeTf = (t: Tf) => {
    setTf(t);
    try {
      window.localStorage.setItem(LS_TF, t);
    } catch {
      /* ignore */
    }
  };
  const changeToggle = (key: keyof Toggles, value: boolean) => {
    const next = { ...toggles, [key]: value };
    setToggles(next);
    try {
      window.localStorage.setItem(LS_TOGGLES, JSON.stringify(next));
    } catch {
      /* ignore */
    }
  };

  const live = state?.live;
  const rows = useMemo(() => levelsTableRows(state), [state]);
  const footStatus = statePoll.error
    ? statePoll.error instanceof ApiError
      ? `state unavailable (HTTP ${statePoll.error.status})`
      : `fetch failed: ${statePoll.error.message}`
    : state
      ? `${state.pair} · ${state.status || "?"} · last bar ${fmtTs(state.last_bar_ts)} · ${(state.levels ?? []).length} levels`
      : pairsPoll.error
        ? "cannot reach API — retrying…"
        : "—";

  return (
    <div className="flex flex-col gap-3" data-testid="desk">
      <h1 className="sr-only" data-testid="page-title">
        Desk
      </h1>
      <div className="flex flex-wrap items-center gap-3">
        <select
          data-testid="pair-select"
          aria-label="pair"
          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
          value={pair ?? ""}
          onChange={(e) => {
            pendingAuto.current = null;
            setPair(e.target.value);
          }}
        >
          {entries.map((e) => (
            <option key={e.pair} value={e.pair}>
              {statusMark(e)} {pairLabel(e.pair)}
            </option>
          ))}
        </select>
        {!adding ? (
          <Button variant="outline" size="sm" data-testid="add-pair-btn" title="добавить актив" onClick={() => setAdding(true)}>
            + добавить
          </Button>
        ) : (
          <span className="inline-flex items-center gap-1.5" data-testid="add-pair-form">
            <textarea
              data-testid="add-pair-input"
              rows={1}
              spellCheck={false}
              autoFocus
              placeholder="LINK/USDT или список через запятую"
              className="h-9 w-60 resize-none rounded-md border border-input bg-background px-3 py-1.5 text-sm"
              value={addText}
              onChange={(e) => setAddText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void submitAdd();
                }
              }}
            />
            <Button variant="outline" size="sm" data-testid="add-pair-go" onClick={() => void submitAdd()}>
              OK
            </Button>
            <Button
              variant="ghost"
              size="sm"
              data-testid="add-pair-cancel"
              onClick={() => {
                setAddErr("");
                setAdding(false);
                setAddText("");
              }}
            >
              ✕
            </Button>
          </span>
        )}
        {addErr && (
          <span className="max-w-sm text-xs text-status-bad" role="alert" data-testid="add-pair-err">
            {addErr}
          </span>
        )}
        <div className="ml-auto flex items-center gap-3">
          <nav className="inline-flex gap-0.5 rounded-md border bg-card p-0.5" aria-label="timeframe" data-testid="tf-switch">
            {TFS.map((t) => (
              <button
                key={t}
                type="button"
                data-testid="tf-btn"
                data-tf={t}
                className={cn(
                  "rounded px-2.5 py-1 text-xs font-semibold transition-colors",
                  t === tf ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:text-foreground",
                )}
                onClick={() => changeTf(t)}
              >
                {t.toUpperCase()}
              </button>
            ))}
          </nav>
          <span
            data-testid="health-dot"
            role="status"
            title={dot?.title ?? "worker health"}
            className={cn("whitespace-nowrap text-xs", dot ? DOT_CLASS[dot.cls] : "text-muted-foreground")}
          >
            {dot?.text ?? "● …"}
          </span>
        </div>
      </div>

      {dot?.banner && (
        <div
          data-testid="stale-banner"
          data-kind={dot.banner.kind}
          className={cn(
            "rounded-md border px-4 py-2 text-sm",
            dot.banner.kind === "info"
              ? "border-status-warn bg-status-warn/10 text-status-warn"
              : "border-status-bad bg-status-bad/10 text-status-bad",
          )}
        >
          {dot.banner.text}
        </div>
      )}

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_300px]">
        <section className="min-w-0 rounded-xl border bg-card p-2">
          <ErrorBoundary
            fallback={(error) => (
              <div className="flex h-[460px] items-center justify-center text-sm text-muted-foreground" data-testid="chart-error">
                график недоступен ({error.message}) — данные ниже работают
              </div>
            )}
          >
            <DeskChart
              viewKey={view.key}
              candles={view.candles}
              zones={zones}
              rangeLines={rangeLines}
              markers={markers}
              live={live}
              tf={tf}
              onNeedOlder={onNeedOlder}
              flash={flash}
            />
          </ErrorBoundary>
          <div className="flex flex-wrap items-center gap-4 px-2 pb-1 pt-2 text-sm text-muted-foreground">
            {TOGGLE_LABELS.map((t) => (
              <label key={t.key} className="inline-flex cursor-pointer items-center gap-1.5" title={t.title}>
                <input
                  type="checkbox"
                  data-testid={t.testId}
                  className="accent-[#3987e5]"
                  checked={toggles[t.key]}
                  onChange={(e) => changeToggle(t.key, e.target.checked)}
                />
                {t.label}
              </label>
            ))}
            <span className="ml-auto inline-flex flex-wrap items-center gap-x-2.5 text-[11px]" aria-label="легенда маркеров">
              <span>
                <span style={{ color: "#199e70" }}>●</span> отбой
              </span>
              <span>
                <span style={{ color: "#e66767" }}>■</span> пробой
              </span>
              <span>
                <span style={{ color: "#3987e5" }}>▲</span> возврат
              </span>
              <span>
                <span style={{ color: "#898781" }}>●</span> касание
              </span>
            </span>
          </div>
        </section>

        <aside className="flex min-w-0 flex-col gap-3">
          <div className="rounded-xl border bg-card px-3.5 py-3">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
              live price{" "}
              <span className={cn("normal-case", live?.ok ? "" : "text-status-warn")} data-testid="live-ok">
                {live ? (live.ok ? "· live" : "· last close") : ""}
              </span>
            </div>
            <div className="mt-0.5 text-3xl font-bold tabular-nums" data-testid="live-price">
              {state ? fmtPrice(live?.ok ? live.price : state.price_last_close_1h) : "—"}
            </div>
            <div className="mt-1.5 truncate text-xs text-muted-foreground" data-testid="live-ts">
              {live?.ts ? `${fmtTsSeconds(live.ts)} (${ageStr(live.ts, now)})` : state ? "—" : "waiting for data…"}
            </div>
          </div>
          <div className="rounded-xl border bg-card px-3.5 py-3 tabular-nums">
            {(["1d", "4h"] as const).map((t) => {
              const trend = state?.trend?.[t] ?? "neutral";
              return (
                <div key={t} className="flex items-baseline gap-2.5 py-0.5">
                  <span className="min-w-16 text-[11px] uppercase tracking-wider text-muted-foreground">trend {t}</span>
                  <span
                    data-testid={`trend-${t}`}
                    className={cn(
                      trend === "up" ? "font-bold text-status-ok" : trend === "down" ? "font-bold text-status-bad" : "text-foreground",
                    )}
                  >
                    {state ? (TREND_ARROW[trend] ?? trend) : "—"}
                  </span>
                  <span className="text-xs text-muted-foreground">{state?.regime?.[t] ?? ""}</span>
                </div>
              );
            })}
            <div className="flex items-baseline gap-2.5 py-0.5">
              <span className="min-w-16 text-[11px] uppercase tracking-wider text-muted-foreground">1H close</span>
              <span data-testid="close-1h">{state ? fmtPrice(state.price_last_close_1h) : "—"}</span>
            </div>
            <div className="mt-1.5 truncate text-xs text-muted-foreground" data-testid="generated-at">
              {state?.generated_at ? `state ${fmtTs(state.generated_at)} (${ageStr(state.generated_at, now)})` : "—"}
            </div>
          </div>
          <div className="flex-1 rounded-xl border bg-card px-3.5 py-3">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">ближайшие уровни</div>
            <div className="mt-1.5 overflow-x-auto">
              <table className="w-full border-collapse text-[12.5px] tabular-nums" data-testid="levels-table">
                <thead>
                  <tr className="text-left text-[10px] uppercase tracking-wider text-muted-foreground">
                    <th className="border-b px-1.5 py-1 font-medium">price</th>
                    <th className="border-b px-1.5 py-1 font-medium">dist</th>
                    <th className="border-b px-1.5 py-1 font-medium">TF</th>
                    <th className="border-b px-1.5 py-1 font-medium">S/R</th>
                    <th className="border-b px-1.5 py-1 font-medium">×</th>
                    <th className="border-b px-1.5 py-1 font-medium">сила</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr
                      key={`${r.zone.price}:${r.zone.top_tf}:${r.zone.kind}`}
                      data-testid="level-row"
                      data-near={r.near ? "1" : "0"}
                      className={cn(
                        "cursor-pointer border-b last:border-b-0 hover:bg-foreground/[0.03]",
                        r.near && "bg-status-warn/10",
                        r.nearest && "font-bold",
                      )}
                      onClick={() => setFlash((f) => ({ price: r.zone.price, nonce: (f?.nonce ?? 0) + 1 }))}
                    >
                      <td className="px-1.5 py-1">{r.priceTxt}</td>
                      <td className="px-1.5 py-1 text-muted-foreground">{r.distTxt}</td>
                      <td className="px-1.5 py-1">{r.tfsTxt}</td>
                      <td className={cn("px-1.5 py-1 font-bold", r.zone.kind === "support" ? "text-status-ok" : "text-status-bad")}>
                        {r.zone.kind === "support" ? "S" : "R"}
                      </td>
                      <td className="px-1.5 py-1 text-muted-foreground">{r.zone.confluence > 1 ? `×${r.zone.confluence}` : ""}</td>
                      <td className={cn("px-1.5 py-1", r.zone.strength === "strong" ? "font-bold" : "text-muted-foreground")}>
                        {r.zone.strength}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </aside>
      </div>

      {bootText && (
        <div className="flex items-center gap-3.5 rounded-lg border bg-card px-4 py-2.5 text-sm text-muted-foreground" data-testid="bootstrap-bar">
          <span data-testid="bootstrap-text">{bootText}</span>
          <span className="h-1.5 min-w-[120px] flex-1 overflow-hidden rounded bg-muted">
            <span className="block h-full rounded bg-[#3987e5] transition-[width]" style={{ width: `${bootPct}%` }} />
          </span>
        </div>
      )}

      <footer className="flex flex-wrap justify-between gap-3 text-xs text-muted-foreground">
        <span data-testid="desk-status">{footStatus}</span>
        <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer" className="hover:text-foreground">
          Charts by TradingView
        </a>
      </footer>
    </div>
  );
}
