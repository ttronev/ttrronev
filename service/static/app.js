/* ttrronev dashboard — vanilla JS, no build step.
   Polls /api/state/{pair} every 15s (levels change on bar close; polling is
   enough — no WebSocket in v1) and /api/health every 60s. */
"use strict";

const POLL_STATE_MS = 15_000;
const POLL_HEALTH_MS = 60_000;
const NEAR_PCT = 1.5;                       // highlight levels closer than this
const TF_ORDER = ["1w", "1d", "4h", "2h", "1h", "5m"];

let currentPair = null;
let stateTimer = null;

const $ = (id) => document.getElementById(id);

function fmtPrice(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const ax = Math.abs(x);
  const digits = ax >= 1000 ? 1 : ax >= 10 ? 2 : ax >= 0.1 ? 4 : 6;
  return "$" + x.toLocaleString("en-US", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}

function ageStr(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/* ------------------------------------------------------------------ hero */
const TREND_ARROW = { up: "▲ up", down: "▼ down", neutral: "◆ neutral" };

function renderHero(st) {
  const live = st.live || {};
  $("live-price").textContent = fmtPrice(live.ok ? live.price : st.price_last_close_1h);
  const okEl = $("live-ok");
  if (live.ok) {
    okEl.textContent = "· live";
    okEl.className = "live-ok";
  } else {
    okEl.textContent = "· last close (live feed down)";
    okEl.className = "live-ok stale";
  }
  $("live-ts").textContent = live.ts ? `${fmtTs(live.ts)} (${ageStr(live.ts)})` : "—";

  for (const tf of ["1d", "4h"]) {
    const t = (st.trend || {})[tf] || "neutral";
    const trendEl = $(`trend-${tf}`);
    trendEl.textContent = TREND_ARROW[t] || t;
    trendEl.className = `tile-value trend ${t}`;
    $(`regime-${tf}`).textContent = `regime: ${(st.regime || {})[tf] || "—"}`;
  }

  $("close-1h").textContent = fmtPrice(st.price_last_close_1h);
  $("generated-at").textContent =
    `state ${fmtTs(st.generated_at)} (${ageStr(st.generated_at)})`;
}

/* ---------------------------------------------------------------- ranges */
function renderRanges(st) {
  const host = $("ranges");
  host.replaceChildren();
  const ranges = st.active_ranges || {};
  const livePx = (st.live && st.live.ok && st.live.price) || st.price_last_close_1h;

  for (const tf of TF_ORDER) {
    if (!(tf in ranges)) continue;
    const row = el("div", "range-row");
    const head = el("div", "range-head");
    head.appendChild(el("span", "range-tf", tf.toUpperCase()));
    const r = ranges[tf];
    if (!r) {
      head.appendChild(el("span", "range-none", "no active range"));
      row.appendChild(head);
      host.appendChild(row);
      continue;
    }
    const [ll, lu] = r.low, [hl, hu] = r.high;
    const span = hu - ll;
    head.appendChild(el("span", "range-zone",
      `${r.zone} · ${r.band_position_pct.toFixed(0)}% of range · ${r.range_id}`));
    row.appendChild(head);
    if (!(span > 0)) {                       // degenerate record — no strip math
      row.appendChild(el("div", "range-none",
        `degenerate range geometry (${fmtPrice(ll)} – ${fmtPrice(hu)})`));
      host.appendChild(row);
      continue;
    }
    const pos = (x) => Math.min(104, Math.max(-4, ((x - ll) / span) * 100));

    const strip = el("div", "strip");
    const segLow = el("div", "seg seg-low");
    segLow.style.left = "0%";
    segLow.style.width = `${Math.max(0, pos(lu))}%`;
    const segHigh = el("div", "seg seg-high");
    segHigh.style.left = `${Math.min(100, pos(hl))}%`;
    segHigh.style.width = `${Math.max(0, 100 - pos(hl))}%`;
    strip.appendChild(segLow);
    strip.appendChild(segHigh);
    if (r.mid) {
      const mid = el("div", "midline");
      mid.style.left = `${pos(r.mid)}%`;
      mid.title = `mid ${fmtPrice(r.mid)}`;
      strip.appendChild(mid);
    }
    const marker = el("div", "marker");
    marker.style.left = `${pos(livePx)}%`;
    marker.title = `price ${fmtPrice(livePx)}`;
    strip.appendChild(marker);
    row.appendChild(strip);

    const labs = el("div", "range-labels");
    labs.appendChild(el("span", "lab-low",
      `low band ${fmtPrice(ll)} – ${fmtPrice(lu)}`));
    labs.appendChild(el("span", "lab-high",
      `high band ${fmtPrice(hl)} – ${fmtPrice(hu)}`));
    row.appendChild(labs);
    host.appendChild(row);
  }
  if (!host.children.length) {
    host.appendChild(el("div", "range-none", "no range data yet"));
  }
}

/* ---------------------------------------------------------------- levels */
function renderLevels(st) {
  const tbody = $("levels-table").querySelector("tbody");
  tbody.replaceChildren();
  const livePx = (st.live && st.live.ok && st.live.price) || st.price_last_close_1h;

  // Distance recomputed client-side against the freshest live price
  // (state.json's order was computed at build time); re-sort to match.
  const levels = (st.levels || []).slice()
    .sort((a, b) => Math.abs(a.price - livePx) - Math.abs(b.price - livePx));
  for (const L of levels) {
    const dist = ((L.price - livePx) / livePx) * 100;
    const tr = el("tr", Math.abs(dist) <= NEAR_PCT ? "near" : "");
    const tdPrice = el("td", "", fmtPrice(L.price));
    if (Math.abs(dist) <= NEAR_PCT) tdPrice.appendChild(el("span", "near-flag", "NEAR"));
    tr.appendChild(tdPrice);
    tr.appendChild(el("td", "dist-pos", `${dist >= 0 ? "+" : ""}${dist.toFixed(2)}%`));
    tr.appendChild(el("td", "", L.tf.toUpperCase()));
    tr.appendChild(el("td", L.kind === "support" ? "kind-s" : "kind-r",
      L.kind === "support" ? "S" : "R"));
    tr.appendChild(el("td", L.strength === "strong" ? "str-strong" : "str-weak",
      L.strength));
    const ev = L.last_event
      ? `${String(L.last_event).replace("historical_level_", "")}` +
        (L.last_event_ts ? ` @ ${String(L.last_event_ts).slice(0, 10)}` : "")
      : "—";
    tr.appendChild(el("td", "ev-tag", ev));
    tbody.appendChild(tr);
  }
  if (!tbody.children.length) {
    const tr = el("tr");
    const td = el("td", "feed-empty", "no strong/weak levels within ±15%");
    td.colSpan = 6;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

/* ---------------------------------------------------------------- events */
function renderEvents(st) {
  const ul = $("events-feed");
  ul.replaceChildren();
  const evs = (st.recent_events_24h || []).slice().reverse();   // newest first
  for (const e of evs) {
    const li = el("li");
    li.appendChild(el("span", "ts", fmtTs(e.ts).slice(5, 17)));
    li.appendChild(el("span", "tf-badge", e.tf.toUpperCase()));
    const t = String(e.type).replace("historical_level_", "");
    li.appendChild(el("span", `etype ${t}`, t));
    li.appendChild(el("span", "price",
      `${fmtPrice(e.level_price)} (close ${fmtPrice(e.bar_close)})`));
    ul.appendChild(li);
  }
  if (!ul.children.length) {
    ul.appendChild(el("li", "feed-empty", "no touched / rejected / broken / reclaimed events in the last 24h"));
  }
}

/* ------------------------------------------------------------------ poll */
function clearPanels(message) {
  $("ranges").replaceChildren(el("div", "range-none", message));
  $("levels-table").querySelector("tbody").replaceChildren();
  $("events-feed").replaceChildren(el("li", "feed-empty", message));
  $("live-price").textContent = "—";
  for (const tf of ["1d", "4h"]) {
    $(`trend-${tf}`).textContent = "—";
    $(`trend-${tf}`).className = "tile-value trend neutral";
    $(`regime-${tf}`).textContent = "—";
  }
  $("close-1h").textContent = "—";
  $("generated-at").textContent = "—";
}

async function refreshState() {
  if (!currentPair) return;
  const pairAtFetch = currentPair;          // guard against pair-switch races
  try {
    const r = await fetch(`/api/state/${pairAtFetch}`, { cache: "no-store" });
    if (pairAtFetch !== currentPair) return;   // user switched mid-flight
    if (!r.ok) {
      // Never leave another pair's data on screen behind this pair's selector.
      clearPanels(`no state for ${pairAtFetch.replace("_", "/")} yet (HTTP ${r.status})`);
      $("foot-status").textContent =
        `state unavailable (HTTP ${r.status}) — worker may still be starting`;
      return;
    }
    const st = await r.json();
    if (pairAtFetch !== currentPair || st.pair !== currentPair) return;
    renderHero(st);
    renderRanges(st);
    renderLevels(st);
    renderEvents(st);
    $("foot-status").textContent =
      `${st.pair} · last bar ${fmtTs(st.last_bar_ts)} · ` +
      `${(st.levels || []).length} levels · refreshed ${fmtTs(new Date().toISOString())}`;
  } catch (err) {
    $("foot-status").textContent = `fetch failed: ${err.message}`;
  }
}

async function refreshHealth() {
  const dot = $("health-dot");
  // Text label always accompanies the color — never color-alone meaning.
  try {
    const r = await fetch("/api/health", { cache: "no-store" });
    const h = await r.json();
    dot.className = `health-dot ${h.ok ? "ok" : "bad"}`;
    dot.textContent = h.ok ? "● worker ok" : "● worker stale";
    dot.title = h.ok
      ? "worker healthy"
      : "worker STALE — freshest 5m regen older than 15 min";
  } catch {
    dot.className = "health-dot bad";
    dot.textContent = "● api down";
    dot.title = "API unreachable";
  }
}

async function loadPairs() {
  const sel = $("pair-select");
  const r = await fetch("/api/pairs", { cache: "no-store" });
  const { pairs } = await r.json();
  sel.replaceChildren();
  for (const p of pairs) {
    const o = el("option", "", p.replace("_", "/"));
    o.value = p;
    sel.appendChild(o);
  }
  return pairs;
}

async function init() {
  let pairs;
  try {
    pairs = await loadPairs();
  } catch {
    // One failed load must not brick the page — retry until the API is up.
    $("foot-status").textContent = "cannot reach API — retrying in 5s…";
    setTimeout(init, 5000);
    return;
  }
  currentPair = pairs[0] || null;
  $("pair-select").addEventListener("change", (e) => {
    currentPair = e.target.value;
    refreshState();
  });
  await refreshState();
  await refreshHealth();
  stateTimer = setInterval(refreshState, POLL_STATE_MS);
  setInterval(refreshHealth, POLL_HEALTH_MS);
}

init();
