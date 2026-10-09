import { describe, expect, it } from "vitest";
import {
  DEFAULT_TOGGLES,
  healthDot,
  levelsTableRows,
  liveBarUpdate,
  mapCandles,
  markerSpecs,
  rangeLineSpecs,
  snapToCandle,
  zoneDraws,
  type Bar,
} from "@/lib/desk/model";
import { MOCK_END_MS, mockCandles, mockState } from "@/mock/api";

const state = mockState("SOL_USDT");
const bars1h: Bar[] = mapCandles(mockCandles("1h", 500));

describe("zones -> price lines (parity with the legacy renderOverlays)", () => {
  it("keeps zones whose top_tf is at or above the view's timeframe", () => {
    expect(zoneDraws(state, "1h", DEFAULT_TOGGLES).map((z) => z.zone.price)).toEqual([98.5, 101.2, 95, 106]);
    expect(zoneDraws(state, "4h", DEFAULT_TOGGLES).map((z) => z.zone.price)).toEqual([98.5, 95, 106]);
    expect(zoneDraws(state, "1w", DEFAULT_TOGGLES)).toEqual([]);
  });

  it("strength toggles filter; a band draws two dashed edges then the labeled centre", () => {
    const onlyWeak = zoneDraws(state, "1h", { ...DEFAULT_TOGGLES, strong: false });
    expect(onlyWeak.map((z) => z.zone.price)).toEqual([101.2]);
    const band = zoneDraws(state, "1h", DEFAULT_TOGGLES)[0];
    expect(band.lines.map((l) => l.price)).toEqual([98.4, 98.6, 98.5]);
    expect(band.lines[2]).toMatchObject({ title: "4H · S ×2", lineWidth: 2, lineStyle: "solid", axisLabelVisible: true });
    expect(band.lines[0]).toMatchObject({ lineStyle: "dashed", axisLabelVisible: false, title: "" });
    const single = zoneDraws(state, "1h", DEFAULT_TOGGLES)[1];
    expect(single.lines).toHaveLength(1);
    expect(single.lines[0]).toMatchObject({ title: "1H · R", lineWidth: 1, lineStyle: "dashed" });
  });

  it("null state draws nothing", () => {
    expect(zoneDraws(null, "1h", DEFAULT_TOGGLES)).toEqual([]);
  });
});

describe("active ranges", () => {
  it("current TF + next higher, four lines each, labels on the outer edges", () => {
    const lines = rangeLineSpecs(state, "1h", DEFAULT_TOGGLES);
    expect(lines.map((l) => l.price)).toEqual([97.8, 98.6, 101.0, 101.6]); // 1h; 2h is null
    expect(lines.map((l) => l.title)).toEqual(["1H range low", "", "", "1H range high"]);
    expect(rangeLineSpecs(state, "4h", DEFAULT_TOGGLES).map((l) => l.price)).toEqual([94.5, 95.5, 105.5, 106.5]);
    expect(rangeLineSpecs(state, "1h", { ...DEFAULT_TOGGLES, ranges: false })).toEqual([]);
  });
});

describe("markers", () => {
  it("snaps events to the candle grid, hides touches unless verbose, sorted by time", () => {
    const m = markerSpecs(state, "1h", DEFAULT_TOGGLES, bars1h);
    expect(m.map((x) => x.shape)).toEqual(["circle", "square"]); // rejected, broken; touched hidden
    expect(m[0].time).toBeLessThan(m[1].time);
    expect(bars1h.some((b) => b.time === m[0].time)).toBe(true);
    const verbose = markerSpecs(state, "1h", { ...DEFAULT_TOGGLES, verboseEvents: true }, bars1h);
    expect(verbose).toHaveLength(3);
    expect(markerSpecs(state, "1h", { ...DEFAULT_TOGGLES, events: false }, bars1h)).toEqual([]);
    // the 4h view hides 1h events but keeps the 1d touch (verbose)
    const bars4h = mapCandles(mockCandles("4h", 500));
    expect(markerSpecs(state, "4h", { ...DEFAULT_TOGGLES, verboseEvents: true }, bars4h)).toHaveLength(1);
  });

  it("snapToCandle is the greatest bar time <= t", () => {
    const bars: Bar[] = [10, 20, 30].map((t) => ({ time: t, open: 1, high: 1, low: 1, close: 1 }));
    expect(snapToCandle(bars, 25)).toBe(20);
    expect(snapToCandle(bars, 30)).toBe(30);
    expect(snapToCandle(bars, 5)).toBeNull();
    expect(snapToCandle([], 5)).toBeNull();
  });
});

describe("live bar", () => {
  const last: Bar = { time: 3600, open: 100, high: 101, low: 99, close: 100.5 };
  it("extends the last bar within its interval", () => {
    expect(liveBarUpdate(last, 101.5, "1h", 3600 + 1800)).toEqual({ time: 3600, open: 100, high: 101.5, low: 99, close: 101.5 });
  });
  it("opens a new bar on the grid anchored to the last real bar", () => {
    expect(liveBarUpdate(last, 99.1, "1h", 3600 + 7300)).toEqual({ time: 3600 + 7200, open: 99.1, high: 99.1, low: 99.1, close: 99.1 });
  });
});

describe("levels table", () => {
  it("sorts by distance to the live price, flags the near band and the nearest row", () => {
    const rows = levelsTableRows(state);
    expect(rows.map((r) => r.zone.price)).toEqual([101.2, 98.5, 95, 106]);
    expect(rows.map((r) => r.near)).toEqual([true, true, false, false]);
    expect(rows[0].nearest).toBe(true);
    expect(rows[1].priceTxt).toBe("$98.40–$98.60");
    expect(rows[0].priceTxt).toBe("$101.20");
    expect(rows[1].tfsTxt).toBe("4H+1H");
    expect(rows[0].distTxt).toBe("+1.20%");
  });

  it("falls back to the last 1h close when the live price is not ok", () => {
    const rows = levelsTableRows({ ...state, live: { price: null, ts: null, ok: false } });
    expect(rows[0].distTxt).toBe("+1.00%"); // 101.2 vs 100.2
    expect(levelsTableRows(null)).toEqual([]);
  });
});

describe("health dot", () => {
  it("live / lag / stale thresholds and the startup info banner", () => {
    expect(healthDot(120, true, "running", 3, 3, false)).toMatchObject({ cls: "ok", text: "● live", banner: null });
    expect(healthDot(600, true, "running", 3, 3, false)).toMatchObject({ cls: "warn", text: "● lag", banner: null });
    expect(healthDot(1000, true, "running", 2, 3, true)).toMatchObject({ cls: "bad", text: "● stale" });
    expect(healthDot(1000, true, "running", 2, 3, true).banner?.kind).toBe("stale");
    const starting = healthDot(null, true, "startup", 1, 3, true);
    expect(starting).toMatchObject({ cls: "warn", text: "● запуск" });
    expect(starting.banner).toEqual({ text: "воркер догоняет данные после запуска: 1/3 пар обновлено", kind: "info" });
    expect(healthDot(null, false, null, 0, 0, true).title).toBe("нет heartbeat");
  });
});

describe("mock candles", () => {
  it("are strictly increasing, end at the fixed end, and page continuously", () => {
    const rows = mockCandles("1h", 500);
    expect(rows).toHaveLength(300);
    expect(rows[rows.length - 1][0]).toBe(MOCK_END_MS - 36e5);
    for (let i = 1; i < rows.length; i++) expect(rows[i][0]).toBeGreaterThan(rows[i - 1][0]);
    const older = mockCandles("1h", 500, rows[0][0]);
    expect(older[older.length - 1][0]).toBeLessThan(rows[0][0]);
  });
});
