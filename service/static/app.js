/* ttrronev dashboard v2 — candlestick chart (TradingView Lightweight Charts
   4.2.0, pinned) + dynamic pair registry. Vanilla JS, no build step.
   Polling only: state 15s, health 30s, candles 60s, bootstrap progress 3s. */
"use strict";

const POLL_STATE_MS = 15_000;
const POLL_HEALTH_MS = 30_000;
const POLL_CANDLES_MS = 60_000;
const POLL_BOOTSTRAP_MS = 3_000;
const NEAR_PCT = 1.5;

const TFS = ["1w", "1d", "4h", "2h", "1h", "5m"];
const TF_RANK = { "5m": 0, "1h": 1, "2h": 2, "4h": 3, "1d": 4, "1w": 5 };
const TF_MS = { "5m": 3e5, "1h": 36e5, "2h": 72e5, "4h": 144e5, "1d": 864e5, "1w": 6048e5 };
const NEXT_UP = { "5m": "1h", "1h": "2h", "2h": "4h", "4h": "1d", "1d": "1w", "1w": null };

const C = {                                   // palette (matches style.css)
  up: "#26a69a", down: "#ef5350",
  support: "#199e70", resist: "#e66767",
  accent: "#3987e5", near: "#fab219",
  text: "#c3c2b7", muted: "#898781", grid: "#2c2c2a", surface: "#1a1a19",
};

let currentPair = null;
let currentTF = localStorage.getItem("ttr_tf") || "1h";
let registryEntries = [];
let lastState = null;
let candleCache = {};                          // "pair:tf" -> {candles, fetchedAt}
let chart, series;
let priceLines = [];                            // [{line, level}]
let rangeLines = [];
let bootstrapTimer = null;
let bootstrapGen = 0;                           // invalidates superseded poll chains
let uiReady = false;                            // one-time DOM setup done
let pendingAutoSwitch = null;                   // pair the USER just added

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

const toggles = Object.assign(
  { strong: true, weak: true, ranges: true, events: true },
  JSON.parse(localStorage.getItem("ttr_toggles") || "{}"));

function fmtPrice(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const ax = Math.abs(x);
  const d = ax >= 1000 ? 1 : ax >= 10 ? 2 : ax >= 0.1 ? 4 : 6;
  return "$" + x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}
function ageStr(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
}

/* ------------------------------------------------------------------ chart */
function initChart() {
  const host = $("chart");
  chart = LightweightCharts.createChart(host, {
    layout: { background: { color: "transparent" }, textColor: C.text },
    grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
    rightPriceScale: { borderColor: C.grid },
    timeScale: { borderColor: C.grid, timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    autoSize: false,
  });
  series = chart.addCandlestickSeries({
    upColor: C.up, downColor: C.down, borderVisible: false,
    wickUpColor: C.up, wickDownColor: C.down,
    priceLineVisible: true, priceLineColor: C.accent,
  });
  const fit = () => chart.resize(host.clientWidth, host.clientHeight);
  new ResizeObserver(fit).observe(host);
  fit();
  chart.timeScale().subscribeVisibleLogicalRangeChange(maybeLoadOlder);
  initPriceAxisZoom(host);
}

/* Price-axis wheel zoom (Stage 8b.5): wheel over the price scale scales the
   auto range around its midpoint; dblclick on the axis resets. */
let priceZoom = 1;

function initPriceAxisZoom(host) {
  series.applyOptions({
    autoscaleInfoProvider: (original) => {
      const res = original();
      if (!res || !res.priceRange || priceZoom === 1) return res;
      const { minValue, maxValue } = res.priceRange;
      const mid = (minValue + maxValue) / 2;
      const half = ((maxValue - minValue) / 2) * priceZoom;
      return { ...res, priceRange: { minValue: mid - half, maxValue: mid + half } };
    },
  });
  const overAxis = (e) => {
    const paneW = host.clientWidth - chart.priceScale("right").width();
    return e.offsetX > paneW;
  };
  host.addEventListener("wheel", (e) => {
    if (!overAxis(e)) return;                  // pane wheel = lib's time zoom
    e.preventDefault();
    priceZoom = Math.min(20, Math.max(0.05, priceZoom * (e.deltaY > 0 ? 1.1 : 1 / 1.1)));
    series.priceScale().applyOptions({ autoScale: true });   // re-run provider
  }, { passive: false });
  host.addEventListener("dblclick", (e) => {
    if (!overAxis(e)) return;
    priceZoom = 1;
    series.priceScale().applyOptions({ autoScale: true });
  });
}

const mapCandles = (candles) => candles.map(([ts, o, h, l, c]) => ({
  time: ts / 1000, open: o, high: h, low: l, close: c }));

async function loadCandles(pair, tf, { force = false } = {}) {
  const key = `${pair}:${tf}`;
  const hit = candleCache[key];
  if (hit && !force) return hit.candles;       // instant TF switch from cache
  const r = await fetch(`/api/candles/${pair}/${tf}?limit=500`, { cache: "no-store" });
  if (!r.ok) throw new Error(`candles HTTP ${r.status}`);
  const j = await r.json();
  const mapped = mapCandles(j.candles);
  if (hit && hit.candles.length) {
    // Keep already-paged-in older history: splice the fresh tail onto it.
    const cut = mapped.length ? mapped[0].time : Infinity;
    const older = hit.candles.filter(b => b.time < cut);
    candleCache[key] = { candles: older.concat(mapped), fetchedAt: Date.now(),
                         hasMore: hit.hasMore, loadingOlder: false };
  } else {
    candleCache[key] = { candles: mapped, fetchedAt: Date.now(),
                         hasMore: !!j.has_more, loadingOlder: false };
  }
  return candleCache[key].candles;
}

// Infinite left-scroll (Stage 8b): when < 100 bars remain left of the view,
// page older candles in and re-setData — the lib keeps the visible TIME
// range, so the viewport doesn't jump. One in-flight request max.
async function maybeLoadOlder(range) {
  if (!range || !currentPair || !series) return;
  const key = `${currentPair}:${currentTF}`;
  const c = candleCache[key];
  if (!c || !c.hasMore || c.loadingOlder || !c.candles.length) return;
  if (range.from >= 100) return;
  c.loadingOlder = true;
  try {
    const before = Math.round(c.candles[0].time * 1000);
    const r = await fetch(
      `/api/candles/${currentPair}/${currentTF}?limit=500&before=${before}`,
      { cache: "no-store" });
    if (!r.ok) { c.hasMore = false; return; }
    const j = await r.json();
    const older = mapCandles(j.candles).filter(b => b.time < c.candles[0].time);
    c.hasMore = !!j.has_more && older.length > 0;
    if (older.length) {
      c.candles = older.concat(c.candles);
      series.setData(c.candles);
      renderOverlays(c.candles);
    }
  } catch { /* transient — retry on next range change */ }
  finally { c.loadingOlder = false; }
}

async function renderChart({ forceCandles = false } = {}) {
  if (!currentPair || !series) return;         // chart may be absent (CDN down)
  let candles;
  try {
    candles = await loadCandles(currentPair, currentTF, { force: forceCandles });
  } catch {
    series.setData([]);
    clearOverlays();
    return;
  }
  series.setData(candles);
  applyLiveToLastBar();
  renderOverlays(candles);
}

function clearOverlays() {
  if (!series) return;
  for (const { line } of priceLines) series.removePriceLine(line);
  for (const line of rangeLines) series.removePriceLine(line);
  priceLines = [];
  rangeLines = [];
  series.setMarkers([]);
  window.__levelsRendered = [];
}

// Snap a timestamp to the bar grid of the loaded candles (greatest candle
// time <= t). Epoch-modulo flooring is WRONG for 1w (the epoch is a
// Thursday; OKX weekly bars are not) — the candles themselves are the grid.
function snapToCandle(candles, tSec) {
  if (!candles.length || tSec < candles[0].time) return null;
  let lo = 0, hi = candles.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (candles[mid].time <= tSec) lo = mid; else hi = mid - 1;
  }
  return candles[lo].time;
}

function renderOverlays(candles) {
  if (!series) return;
  clearOverlays();
  if (!lastState || lastState.pair !== currentPair) return;
  const rank = TF_RANK[currentTF];

  // Levels -> priceLines. Higher-TF levels always visible; lower-TF hidden.
  for (const L of lastState.levels || []) {
    if (TF_RANK[L.tf] < rank) continue;
    if (L.strength === "strong" && !toggles.strong) continue;
    if (L.strength === "weak" && !toggles.weak) continue;
    const isS = L.kind === "support";
    const line = series.createPriceLine({
      price: L.price,
      color: isS ? C.support : C.resist,
      lineWidth: L.strength === "strong" ? 2 : 1,
      lineStyle: L.strength === "strong"
        ? LightweightCharts.LineStyle.Solid : LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title: `${L.tf.toUpperCase()} · ${isS ? "S" : "R"}`,
    });
    priceLines.push({ line, level: L });
  }
  window.__levelsRendered = priceLines.map(p => p.level.price);   // acceptance hook

  // Active range bands: current TF + next higher.
  if (toggles.ranges) {
    const tfsToShow = [currentTF, NEXT_UP[currentTF]].filter(Boolean);
    for (const tf of tfsToShow) {
      const r = (lastState.active_ranges || {})[tf];
      if (!r) continue;
      const mk = (price, color, title) => rangeLines.push(series.createPriceLine({
        price, color, lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: false, title,
      }));
      mk(r.low[0], C.support, `${tf.toUpperCase()} range low`);
      mk(r.low[1], C.support, "");
      mk(r.high[0], C.resist, "");
      mk(r.high[1], C.resist, `${tf.toUpperCase()} range high`);
    }
  }

  // Event markers, last 7 days, same TF-visibility rule as levels.
  if (toggles.events && candles.length) {
    const first = candles[0].time, last = candles[candles.length - 1].time;
    const MARK = {
      touched:   { shape: "circle", color: C.muted, position: "inBar" },
      rejected:  { shape: "circle", color: C.support, position: "belowBar" },
      broken:    { shape: "square", color: C.resist, position: "aboveBar" },
      reclaimed: { shape: "arrowUp", color: C.accent, position: "belowBar" },
    };
    const markers = [];
    for (const e of lastState.recent_events_7d || []) {
      if (TF_RANK[e.tf] < rank) continue;
      const t = String(e.type).replace("historical_level_", "");
      const m = MARK[t];
      if (!m) continue;
      const barT = snapToCandle(candles, new Date(e.ts).getTime() / 1000);
      if (barT === null || barT < first || barT > last) continue;
      markers.push({ time: barT, position: m.position, shape: m.shape,
                     color: m.color, text: `${t} ${fmtPrice(e.level_price)}` });
    }
    markers.sort((a, b) => a.time - b.time);
    series.setMarkers(markers);
  }
}

function applyLiveToLastBar() {
  // Approximate the forming bar from live.json; the real close rewrites it.
  // The bar grid is anchored to the LAST REAL BAR, not the epoch (epoch-
  // modulo puts 1w boundaries on Thursdays and would corrupt weekly bars).
  const live = lastState && lastState.live;
  if (!live || !live.ok || !live.price || !series) return;
  const key = `${currentPair}:${currentTF}`;
  const cached = candleCache[key];
  if (!cached || !cached.candles.length) return;
  const lastBar = cached.candles[cached.candles.length - 1];
  const px = live.price;
  const stepS = TF_MS[currentTF] / 1000;
  const nowS = Date.now() / 1000;
  const ahead = Math.max(0, Math.floor((nowS - lastBar.time) / stepS));
  if (ahead > 0) {
    series.update({ time: lastBar.time + ahead * stepS,
                    open: px, high: px, low: px, close: px });
  } else {
    series.update({ ...lastBar, close: px,
                    high: Math.max(lastBar.high, px), low: Math.min(lastBar.low, px) });
  }
}

function flashLevel(price) {
  const hit = priceLines.find(p => p.level.price === price);
  if (!hit) return;
  const orig = {
    color: hit.level.kind === "support" ? C.support : C.resist,
    lineWidth: hit.level.strength === "strong" ? 2 : 1,
  };
  hit.line.applyOptions({ color: C.near, lineWidth: 3 });
  setTimeout(() => hit.line.applyOptions(orig), 900);
}

/* ------------------------------------------------------------- state card */
const TREND_ARROW = { up: "▲ up", down: "▼ down", neutral: "◆ neutral" };

function renderStateCard(st) {
  const live = st.live || {};
  $("live-price").textContent = fmtPrice(live.ok ? live.price : st.price_last_close_1h);
  const okEl = $("live-ok");
  okEl.textContent = live.ok ? "· live" : "· last close";
  okEl.className = live.ok ? "live-ok" : "live-ok stale";
  $("live-ts").textContent = live.ts ? `${fmtTs(live.ts)} (${ageStr(live.ts)})` : "—";
  for (const tf of ["1d", "4h"]) {
    const t = (st.trend || {})[tf] || "neutral";
    const trendEl = $(`trend-${tf}`);
    trendEl.textContent = TREND_ARROW[t] || t;
    trendEl.className = `trend ${t}`;
    $(`regime-${tf}`).textContent = (st.regime || {})[tf] || "";
  }
  $("close-1h").textContent = fmtPrice(st.price_last_close_1h);
  $("generated-at").textContent =
    st.generated_at ? `state ${fmtTs(st.generated_at)} (${ageStr(st.generated_at)})` : "—";
}

function renderLevelsTable(st) {
  const tbody = $("levels-table").querySelector("tbody");
  tbody.replaceChildren();
  const live = st.live || {};
  const livePx = (live.ok && live.price) || st.price_last_close_1h;
  if (!livePx) return;
  const levels = (st.levels || []).slice()
    .sort((a, b) => Math.abs(a.price - livePx) - Math.abs(b.price - livePx));
  for (const L of levels.slice(0, 14)) {
    const dist = ((L.price - livePx) / livePx) * 100;
    const tr = el("tr", Math.abs(dist) <= NEAR_PCT ? "near" : "");
    tr.appendChild(el("td", "", fmtPrice(L.price)));
    tr.appendChild(el("td", "dist-pos", `${dist >= 0 ? "+" : ""}${dist.toFixed(2)}%`));
    tr.appendChild(el("td", "", L.tf.toUpperCase()));
    tr.appendChild(el("td", L.kind === "support" ? "kind-s" : "kind-r",
      L.kind === "support" ? "S" : "R"));
    tr.appendChild(el("td", L.strength === "strong" ? "str-strong" : "str-weak", L.strength));
    tr.addEventListener("click", () => flashLevel(L.price));
    tbody.appendChild(tr);
  }
  if (levels.length && Math.abs(((levels[0].price - livePx) / livePx) * 100) <= NEAR_PCT)
    tbody.firstChild.classList.add("nearest");
}

/* --------------------------------------------------------------- registry */
function statusMark(e) {
  return e.status === "ready" ? "●" : e.status === "bootstrapping" ? "◌" : "✕";
}

function renderPairSelect() {
  const sel = $("pair-select");
  const prev = sel.value;
  sel.replaceChildren();
  for (const e of registryEntries) {
    const o = el("option", "", `${statusMark(e)} ${e.pair.replace("_", "/")}`);
    o.value = e.pair;
    sel.appendChild(o);
  }
  if (registryEntries.some(e => e.pair === (prev || currentPair)))
    sel.value = prev || currentPair;
}

async function loadRegistry() {
  const r = await fetch("/api/pairs", { cache: "no-store" });
  const j = await r.json();
  registryEntries = j.registry || [];
  renderPairSelect();
  scheduleBootstrapPoll();
  return registryEntries;
}

/* ------------------------------------------------------------- bootstrap */
function scheduleBootstrapPoll() {
  const boot = registryEntries.filter(e => e.status === "bootstrapping")
    .sort((a, b) => (a.added_ts || "").localeCompare(b.added_ts || ""));
  const bar = $("bootstrap-bar");
  // A fired timer can't be cancelled and an in-flight poll will still try
  // to reschedule — the generation token makes superseded chains no-ops.
  const gen = ++bootstrapGen;
  if (bootstrapTimer) { clearTimeout(bootstrapTimer); bootstrapTimer = null; }
  if (!boot.length) {
    bar.hidden = true;
    // The ready transition may be observed HERE (periodic registry refresh
    // superseding the 3s poll) — honor the pending auto-switch either way.
    const target = pendingAutoSwitch;
    if (target && registryEntries.some(e => e.pair === target && e.status === "ready")) {
      pendingAutoSwitch = null;
      currentPair = target;
      $("pair-select").value = target;
      delete candleCache[`${target}:${currentTF}`];
      refreshState().then(() => renderChart({ forceCandles: true }));
    }
    return;
  }
  bar.hidden = false;
  const active = boot[0];
  const queued = boot.slice(1).map(e => e.pair.replace("_", "/"));
  const poll = async () => {
    if (gen !== bootstrapGen) return;
    try {
      const r = await fetch(`/api/state/${active.pair}`, { cache: "no-store" });
      const st = await r.json();
      if (gen !== bootstrapGen) return;
      const n = (st.tfs_ready || []).length;
      let txt = `загрузка ${active.pair.replace("_", "/")}: ${n}/6 TF`;
      if (queued.length) txt += ` · в очереди: ${queued.join(", ")}`;
      $("bootstrap-text").textContent = txt;
      $("bootstrap-fill").style.width = `${Math.round(n / 6 * 100)}%`;
      if (st.status === "ready") {
        const autoTarget = pendingAutoSwitch === active.pair ? active.pair : null;
        pendingAutoSwitch = null;
        await loadRegistry();                  // re-runs this scheduler for the queue
        if (autoTarget) {
          // Switch only to the pair THIS user added — never yank the view
          // away from a pair they navigated to meanwhile.
          currentPair = autoTarget;
          $("pair-select").value = currentPair;
          delete candleCache[`${autoTarget}:${currentTF}`];
          await refreshState();
          await renderChart({ forceCandles: true });
        }
        return;
      }
      if (st.status === "error") {
        $("bootstrap-text").textContent =
          `ошибка загрузки ${active.pair.replace("_", "/")}: ${st.error_reason || "unknown"} (повторите добавление)`;
        $("bootstrap-fill").style.width = "0%";
        await loadRegistry();
        return;
      }
    } catch { /* API momentarily away — keep polling */ }
    if (gen !== bootstrapGen) return;
    bootstrapTimer = setTimeout(poll, POLL_BOOTSTRAP_MS);
  };
  poll();
}

async function addPair(input) {
  // Comma/newline-separated list -> batch POST (Stage 8b). Spaces stay
  // inside a symbol ("link usdt" is one symbol).
  const err = $("add-pair-err");
  err.textContent = "";
  const symbols = input.split(/[,\n;]+/).map(s => s.trim()).filter(Boolean);
  if (!symbols.length) return false;
  const body = symbols.length === 1 ? { symbol: symbols[0] } : { symbols };
  try {
    const r = await fetch("/api/pairs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = j.detail;
      err.textContent = typeof d === "string" ? d
        : d && d.rejected ? d.rejected.map(x => `${x.symbol}: ${x.reason}`).join("; ")
        : `HTTP ${r.status}`;
      return false;
    }
    const queued = j.queued || [];
    const rejected = j.rejected || [];
    if (rejected.length) {
      err.textContent = `отклонено: ` +
        rejected.map(x => `${x.symbol} (${x.reason})`).join("; ");
    }
    pendingAutoSwitch = queued.length === 1 ? queued[0] : null;
    await loadRegistry();
    return queued.length > 0;
  } catch (e) {
    err.textContent = `сеть: ${e.message}`;
    return false;
  }
}

/* ------------------------------------------------------------------ polls */
async function refreshState() {
  if (!currentPair) return;
  const pairAtFetch = currentPair;
  try {
    const r = await fetch(`/api/state/${pairAtFetch}`, { cache: "no-store" });
    if (pairAtFetch !== currentPair) return;
    if (!r.ok) {
      $("foot-status").textContent = `state unavailable (HTTP ${r.status})`;
      return;
    }
    const st = await r.json();
    if (pairAtFetch !== currentPair) return;
    lastState = st;
    renderStateCard(st);
    renderLevelsTable(st);
    applyLiveToLastBar();
    // Re-render overlays when a new bar landed (levels/events may change).
    renderOverlays((candleCache[`${currentPair}:${currentTF}`] || { candles: [] }).candles);
    $("foot-status").textContent =
      `${st.pair} · ${st.status || "?"} · last bar ${fmtTs(st.last_bar_ts)} · ` +
      `${(st.levels || []).length} levels`;
  } catch (e) {
    $("foot-status").textContent = `fetch failed: ${e.message}`;
  }
}

async function refreshHealth() {
  const dot = $("health-dot");
  const banner = $("stale-banner");
  try {
    const r = await fetch("/api/health", { cache: "no-store" });
    const h = await r.json();
    const mine = (h.pairs || {})[currentPair];
    const age = mine ? mine.regen_5m_age_s : null;
    let cls, txt;
    if (age !== null && age !== undefined && age < 300) { cls = "ok"; txt = "● live"; }
    else if (age !== null && age !== undefined && age < 900) { cls = "warn"; txt = "● lag"; }
    else { cls = "bad"; txt = "● stale"; }
    dot.className = `health-dot ${cls}`;
    dot.textContent = txt;
    dot.title = age !== null && age !== undefined
      ? `последний 5m-реген: ${Math.round(age / 60)} мин назад` : "нет heartbeat";
    banner.hidden = cls !== "bad";
  } catch {
    dot.className = "health-dot bad";
    dot.textContent = "● api down";
    banner.hidden = false;
  }
}

async function refreshCandles() {
  if (!currentPair) return;
  await renderChart({ forceCandles: true }).catch(() => {});
}

/* -------------------------------------------------------------------- UI */
function initTFSwitch() {
  const nav = $("tf-switch");
  for (const tf of TFS) {
    const b = el("button", tf === currentTF ? "tf-btn active" : "tf-btn", tf.toUpperCase());
    b.addEventListener("click", async () => {
      currentTF = tf;
      localStorage.setItem("ttr_tf", tf);
      nav.querySelectorAll(".tf-btn").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      await renderChart();                     // cached candles -> instant
    });
    nav.appendChild(b);
  }
}

function initToggles() {
  const map = { "tg-strong": "strong", "tg-weak": "weak", "tg-ranges": "ranges", "tg-events": "events" };
  for (const [id, key] of Object.entries(map)) {
    const cb = $(id);
    cb.checked = toggles[key];
    cb.addEventListener("change", () => {
      toggles[key] = cb.checked;
      localStorage.setItem("ttr_toggles", JSON.stringify(toggles));
      renderOverlays((candleCache[`${currentPair}:${currentTF}`] || { candles: [] }).candles);
    });
  }
}

function initAddPair() {
  $("add-pair-btn").addEventListener("click", () => {
    $("add-pair-form").hidden = false;
    $("add-pair-btn").hidden = true;
    $("add-pair-input").focus();
  });
  const close = () => {
    $("add-pair-form").hidden = true;
    $("add-pair-btn").hidden = false;
    $("add-pair-input").value = "";
  };
  $("add-pair-cancel").addEventListener("click", () => { $("add-pair-err").textContent = ""; close(); });
  const go = async () => {
    const v = $("add-pair-input").value.trim();
    if (!v) return;
    if (await addPair(v)) close();
  };
  $("add-pair-go").addEventListener("click", go);
  $("add-pair-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); }
  });
}

function initOnce() {
  // One-time DOM/chart setup — must NOT re-run on API-retry, or every retry
  // stacks another chart instance and duplicate listeners.
  if (uiReady) return;
  uiReady = true;
  try {
    initChart();                 // CDN may be down: page degrades to panels
  } catch {
    $("foot-status").textContent =
      "график недоступен (библиотека не загрузилась) — данные ниже работают";
  }
  initTFSwitch();
  initToggles();
  initAddPair();
  $("pair-select").addEventListener("change", async (e) => {
    currentPair = e.target.value;
    pendingAutoSwitch = null;                  // user navigated: no auto-yank
    lastState = null;
    await refreshState();
    await renderChart();
  });
}

async function init() {
  initOnce();
  try {
    await loadRegistry();
  } catch {
    $("foot-status").textContent = "cannot reach API — retrying in 5s…";
    setTimeout(init, 5000);
    return;
  }
  const ready = registryEntries.filter(e => e.status === "ready");
  currentPair = (ready[0] || registryEntries[0] || {}).pair || null;
  if (currentPair) $("pair-select").value = currentPair;
  await refreshState();
  await renderChart();
  await refreshHealth();
  setInterval(refreshState, POLL_STATE_MS);
  setInterval(refreshHealth, POLL_HEALTH_MS);
  setInterval(refreshCandles, POLL_CANDLES_MS);
  setInterval(loadRegistry, POLL_BOOTSTRAP_MS * 4);   // pick up external registry edits
}

init();
