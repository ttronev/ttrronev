// The Desk chart: TradingView Lightweight Charts 4.2.0 (pinned, Apache-2.0,
// attribution in the Desk footer), driven imperatively exactly like the
// legacy page: candles + forming bar from the live price, zones as bands,
// active-range lines, event markers, infinite left scroll, price-axis wheel
// zoom. All selection logic lives in src/lib/desk/model.ts; this file only
// paints what it is given.
import {
  CrosshairMode,
  LineStyle,
  createChart,
  type AutoscaleInfo,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LogicalRange,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";
import { priceDecimals } from "@/lib/desk/format";
import {
  PALETTE,
  liveBarUpdate,
  type Bar,
  type Live,
  type MarkerSpec,
  type PriceLineSpec,
  type Tf,
  type ZoneDraw,
} from "@/lib/desk/model";
import { cn } from "@/lib/utils";

export interface DeskChartProps {
  /** `${pair}:${tf}`: a change resets axis zoom and lands on the last ~150 bars. */
  viewKey: string;
  candles: Bar[];
  zones: ZoneDraw[];
  rangeLines: PriceLineSpec[];
  markers: MarkerSpec[];
  live: Live | null | undefined;
  tf: Tf;
  /** Fewer than 100 bars left of the view: page older candles in. */
  onNeedOlder?: () => void;
  /** Highlight the zone drawn at this price (table row click). */
  flash?: { price: number; nonce: number } | null;
  className?: string;
}

/** hsl(h, s%, l%) -> #rrggbb. Lightweight Charts parses only hex, rgb(a)
 *  and named colours, never hsl(). */
export function hslToHex(h: number, s: number, l: number): string {
  const S = s / 100;
  const L = l / 100;
  const C = (1 - Math.abs(2 * L - 1)) * S;
  const Hp = (((h % 360) + 360) % 360) / 60;
  const X = C * (1 - Math.abs((Hp % 2) - 1));
  let rgb: [number, number, number];
  if (Hp < 1) rgb = [C, X, 0];
  else if (Hp < 2) rgb = [X, C, 0];
  else if (Hp < 3) rgb = [0, C, X];
  else if (Hp < 4) rgb = [0, X, C];
  else if (Hp < 5) rgb = [X, 0, C];
  else rgb = [C, 0, X];
  const m = L - C / 2;
  const hex = (v: number) =>
    Math.max(0, Math.min(255, Math.round((v + m) * 255)))
      .toString(16)
      .padStart(2, "0");
  return `#${hex(rgb[0])}${hex(rgb[1])}${hex(rgb[2])}`;
}

/** A shadcn token (`H S% L%` triplet on :root / .dark) as a hex colour the
 *  chart library accepts; `fallback` when the token is missing or malformed. */
export function cssHsl(
  name: string,
  fallback: string,
  root: HTMLElement | null = typeof document === "undefined" ? null : document.documentElement,
): string {
  try {
    if (!root) return fallback;
    const parts = getComputedStyle(root).getPropertyValue(name).trim().split(/\s+/);
    if (parts.length !== 3 || !/^-?[\d.]+$/.test(parts[0]) || !/^[\d.]+%$/.test(parts[1]) || !/^[\d.]+%$/.test(parts[2])) {
      return fallback;
    }
    return hslToHex(parseFloat(parts[0]), parseFloat(parts[1]), parseFloat(parts[2]));
  } catch {
    return fallback;
  }
}

function themeColors() {
  return { text: cssHsl("--muted-foreground", PALETTE.text), grid: cssHsl("--border", PALETTE.grid) };
}

const toData = (b: Bar): CandlestickData<UTCTimestamp> => ({
  time: b.time as UTCTimestamp,
  open: b.open,
  high: b.high,
  low: b.low,
  close: b.close,
});

const lineStyle = (s: PriceLineSpec["lineStyle"]) => (s === "solid" ? LineStyle.Solid : LineStyle.Dashed);

function createLine(series: ISeriesApi<"Candlestick">, s: PriceLineSpec): IPriceLine {
  return series.createPriceLine({
    price: s.price,
    color: s.color,
    lineWidth: s.lineWidth,
    lineStyle: lineStyle(s.lineStyle),
    axisLabelVisible: s.axisLabelVisible,
    title: s.title,
  });
}

function applyLive(series: ISeriesApi<"Candlestick">, candles: readonly Bar[], live: Live | null | undefined, tf: Tf) {
  if (!live || !live.ok || !live.price || !candles.length) return;
  const last = candles[candles.length - 1];
  try {
    series.update(toData(liveBarUpdate(last, live.price, tf, Date.now() / 1000)));
  } catch {
    /* a bar older than the series' last point is rejected by the library; ignore */
  }
}

export function DeskChart({ viewKey, candles, zones, rangeLines, markers, live, tf, onNeedOlder, flash, className }: DeskChartProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const zoneLinesRef = useRef<{ zone: ZoneDraw["zone"]; spec: PriceLineSpec[]; lines: IPriceLine[] }[]>([]);
  const rangeLinesRef = useRef<IPriceLine[]>([]);
  const priceZoomRef = useRef(1);
  const lastViewKeyRef = useRef<string | null>(null);
  const candlesRef = useRef<Bar[]>(candles);
  candlesRef.current = candles;
  const onNeedOlderRef = useRef(onNeedOlder);
  onNeedOlderRef.current = onNeedOlder;

  // One-time chart setup.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const colors = themeColors();
    const chart = createChart(host, {
      layout: { background: { color: "transparent" }, textColor: colors.text },
      grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
      rightPriceScale: { borderColor: colors.grid },
      timeScale: { borderColor: colors.grid, timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      autoSize: false,
    });
    const series = chart.addCandlestickSeries({
      upColor: PALETTE.up,
      downColor: PALETTE.down,
      borderVisible: false,
      wickUpColor: PALETTE.up,
      wickDownColor: PALETTE.down,
      priceLineVisible: true,
      priceLineColor: PALETTE.accent,
      // Price-axis wheel zoom: scale the auto range around its midpoint.
      autoscaleInfoProvider: (original: () => AutoscaleInfo | null) => {
        const res = original();
        if (!res || !res.priceRange || priceZoomRef.current === 1) return res;
        const { minValue, maxValue } = res.priceRange;
        const mid = (minValue + maxValue) / 2;
        const half = ((maxValue - minValue) / 2) * priceZoomRef.current;
        return { ...res, priceRange: { minValue: mid - half, maxValue: mid + half } };
      },
    });
    const fit = () => chart.resize(host.clientWidth, host.clientHeight);
    const ro = new ResizeObserver(fit);
    ro.observe(host);
    fit();
    const onRange = (range: LogicalRange | null) => {
      if (range && range.from < 100) onNeedOlderRef.current?.();
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRange);
    const overAxis = (e: MouseEvent) => e.offsetX > host.clientWidth - chart.priceScale("right").width();
    const onWheel = (e: WheelEvent) => {
      if (!overAxis(e)) return; // pane wheel = the library's time zoom
      e.preventDefault();
      priceZoomRef.current = Math.min(20, Math.max(0.05, priceZoomRef.current * (e.deltaY > 0 ? 1.1 : 1 / 1.1)));
      series.priceScale().applyOptions({ autoScale: true });
    };
    const onDbl = (e: MouseEvent) => {
      if (!overAxis(e)) return;
      priceZoomRef.current = 1;
      series.priceScale().applyOptions({ autoScale: true });
    };
    host.addEventListener("wheel", onWheel, { passive: false });
    host.addEventListener("dblclick", onDbl);
    // Follow the system theme (the html `dark` class).
    const mo = new MutationObserver(() => {
      const c = themeColors();
      chart.applyOptions({
        layout: { textColor: c.text },
        grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
        rightPriceScale: { borderColor: c.grid },
        timeScale: { borderColor: c.grid },
      });
    });
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    chartRef.current = chart;
    seriesRef.current = series;
    lastViewKeyRef.current = null;
    return () => {
      mo.disconnect();
      ro.disconnect();
      host.removeEventListener("wheel", onWheel);
      host.removeEventListener("dblclick", onDbl);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      zoneLinesRef.current = [];
      rangeLinesRef.current = [];
    };
  }, []);

  // Candles. A new view resets the manual axis zoom BEFORE setData, forces
  // price autoscale back on and lands on the last ~150 bars (not
  // fitContent). An in-view refresh keeps the viewport: the library holds
  // the visible time range across setData.
  useEffect(() => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!chart || !series) return;
    const isNewView = lastViewKeyRef.current !== viewKey;
    if (isNewView) priceZoomRef.current = 1;
    if (candles.length) {
      const d = priceDecimals(candles[candles.length - 1].close);
      series.applyOptions({ priceFormat: { type: "price", precision: d, minMove: Math.pow(10, -d) } });
    }
    series.setData(candles.map(toData));
    applyLive(series, candles, live, tf);
    if (isNewView) {
      lastViewKeyRef.current = viewKey;
      series.priceScale().applyOptions({ autoScale: true });
      chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, candles.length - 150), to: candles.length + 5 });
    }
    // live/tf are applied by the effect below; listing them here would re-run setData.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewKey, candles]);

  // Overlays: zones (bands), active-range lines, event markers.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    for (const zl of zoneLinesRef.current) for (const l of zl.lines) series.removePriceLine(l);
    for (const l of rangeLinesRef.current) series.removePriceLine(l);
    zoneLinesRef.current = zones.map((z) => ({ zone: z.zone, spec: z.lines, lines: z.lines.map((s) => createLine(series, s)) }));
    rangeLinesRef.current = rangeLines.map((s) => createLine(series, s));
    series.setMarkers(
      markers.map((m) => ({ time: m.time as UTCTimestamp, position: m.position, shape: m.shape, color: m.color })) as SeriesMarker<Time>[],
    );
  }, [zones, rangeLines, markers]);

  // The forming bar from the live price.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    applyLive(series, candlesRef.current, live, tf);
  }, [live, tf]);

  // Flash a zone: every line of the band, then restore widths/colours.
  useEffect(() => {
    if (!flash) return;
    const hit = zoneLinesRef.current.find((zl) => zl.zone.price === flash.price);
    if (!hit) return;
    for (const l of hit.lines) l.applyOptions({ color: PALETTE.near, lineWidth: 3 });
    const t = window.setTimeout(() => {
      try {
        hit.lines.forEach((l, i) => l.applyOptions({ color: hit.spec[i].color, lineWidth: hit.spec[i].lineWidth }));
      } catch {
        /* lines may have been replaced meanwhile */
      }
    }, 900);
    return () => window.clearTimeout(t);
  }, [flash]);

  return <div ref={hostRef} data-testid="desk-chart" className={cn("h-[460px] w-full", className)} />;
}
