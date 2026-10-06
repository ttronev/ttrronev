"""
detectors/alerts.py — structural alerts over the layer-5 range-memory events.

The information layer identifies old levels being respected (range memory) and
near-price structure, but the user kept MISSING setups because nothing
interrupts them. This fires Telegram DMs when something structurally meaningful
happens near current price.

Memory hygiene: see MEMORY_HYGIENE.md (peak-RSS self-report via shared/memhygiene;
gc between watch iterations; clean Ctrl+C). Freshness contract: see
MEMORY_HYGIENE.md — this is an END-CONSUMER, so it auto-fetches + cascade-regens
on startup unless --no-freshness / --no-regen.

Trigger conditions (any):
  1. touched   on a STRONG level within +/-2% of current price (any TF)
  2. rejected  (strong OR weak) within +/-3% of current price (any TF)
  3. broken    on a STRONG level (any TF)
  4. reclaimed on a STRONG level (any TF; reclaim implies a prior break)
  5. active 1D/1W range_low or range_high broken AND held >= recovery_lookahead bars
  6. confluence: 2+ touched events within 4h at prices within +/-0.5% of each other
  7. proximity: live spot within --proximity-pct of a strong/weak level, even with
     NO event yet — an early "approaching, prepare" heads-up. Threshold tuning
     (--proximity-pct, in PERCENT): 0.5 = "at level, about to touch" /
     1.5 = "approaching, prepare" (default) / 2.5 = "in the zone, anticipate".

Only RECENT events fire (within --recency-hours of the latest bar, default 4h);
this caps first-run spam from stale events AND keeps ongoing alerts relevant.
Dedup keeps the same (tf, level, event) from re-firing within 4h, persisted
across restarts.

Telegram: DM via env creds (TTRRONEV_TG_BOT_TOKEN / TTRRONEV_TG_CHAT_ID, loaded
from .env). The token is NEVER printed/logged.
"""
from __future__ import annotations
import argparse, gc, json, os, sys, time
import urllib.request, urllib.error
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detectors import paths
from shared.pricefmt import fmt_price, price_key   # scale-safe text + dedup keys

TFS = ["1w", "1d", "4h", "2h", "1h"]
_STATE_PATH = lambda pair=paths.DEFAULT_PAIR: paths.alerts_state(pair)
_CSV = lambda tf, pair=paths.DEFAULT_PAIR: paths.raw_csv(tf, pair)

TOUCH_PROX = 0.02            # condition 1: +/-2%
REJECT_PROX = 0.03           # condition 2: +/-3%
PROXIMITY_THRESHOLD_PCT = 1.5    # proximity alert threshold, IN PERCENT (1.5 => +/-1.5%,
                            # ~ +/-$1.05 on $70). NB: a PERCENT, unlike TOUCH_PROX /
                            # REJECT_PROX above which are fractions. Per-run override:
                            # --proximity-pct. Tuning: 0.5 = "at level, about to touch" /
                            # 1.5 = "approaching, prepare" (default) / 2.5 = "in the zone".
                            # Fires BEFORE any touched/rejected/broken event prints.
ALERT_RECENCY_HOURS = 4      # recency filter: only fire on events within this many
                            # hours of the latest bar (caps first-run spam from
                            # stale events AND keeps ongoing alerts relevant)
RECENCY_BARS = 2             # ...but a 4H/1D event from its latest CLOSED bar is
                            # already >= its bar interval old, so the effective
                            # window is max(ALERT_RECENCY_HOURS, RECENCY_BARS*bar)
BAR_HOURS = {"1w": 168, "1d": 24, "4h": 4, "2h": 2, "1h": 1}
DEDUP_HOURS = 4              # don't re-fire same (tf, level, event) within this
CONFLUENCE_WINDOW_H = 4      # condition 6 time window
CONFLUENCE_PROX = 0.005      # condition 6 price proximity (+/-0.5%)
WATCH_INTERVAL_S = 5 * 60    # --watch loop period default (override: --interval-seconds)


# ----------------------------------------------------------------- telegram
def _load_dotenv():
    """Populate os.environ from the repo .env (only keys not already set).
    No external dependency; never echoes values."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _deliver(text: str, dry_run: bool, log=print) -> str:
    """Deliver one message. Returns
        'sent'     Telegram accepted it;
        'printed'  intentionally NOT sent — dry-run, or no creds configured
                   (log-only mode): printing IS the delivery channel;
        'failed'   a send was attempted and did not succeed.
    Callers must treat 'failed' as not-delivered (retry later), never as done.
    The bot token is never logged."""
    if dry_run:
        log("---- WOULD SEND ----\n" + text + "\n--------------------")
        return "printed"
    token = os.environ.get("TTRRONEV_TG_BOT_TOKEN")
    chat = os.environ.get("TTRRONEV_TG_CHAT_ID")
    if not token or not chat:
        log("[alerts] no Telegram creds in env -> printing instead:\n" + text)
        return "printed"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat, "text": text,
                          "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return "sent" if resp.status == 200 else "failed"
    except Exception as e:                      # never echo the URL (has token)
        log(f"[alerts] telegram send failed: {type(e).__name__}")
        return "failed"


def _telegram_send(text: str, dry_run: bool, log=print) -> bool:
    """Bool wrapper kept for existing callers (worker error DMs,
    --test-telegram): True if sent, or printed by an explicit dry-run."""
    status = _deliver(text, dry_run, log=log)
    return status == "sent" or (dry_run and status == "printed")


# --------------------------------------------------------------- scanning
def _recent_closes(tf, n, pair=paths.DEFAULT_PAIR):
    from shared.csvtail import read_tail          # tail only, not the whole file
    df = read_tail(_CSV(tf, pair), rows=n, columns=["close"])
    out = df["close"].to_numpy(float)[-n:]
    del df
    return out


def _range_break_alert(state, tf):
    """Condition 5: active 1D/1W range edge broken and HELD >= recovery_lookahead
    bars (a confirmed structural break, not a one-bar poke)."""
    r = state.active_range(tf)
    if not r:
        return None
    price = state.live_price           # position check vs live spot; confirmation uses closes below
    ll, hu = r["range_low_lower"], r["range_high_upper"]
    rl = int(state._l1[tf]["config"]["recovery_lookahead"])
    closes = _recent_closes(tf, rl + 2, getattr(state, "pair", paths.DEFAULT_PAIR))
    if len(closes) < rl:
        return None
    win = closes[-rl:]
    if price < ll and bool((win < ll).all()):
        side, lvl = "low", ll
    elif price > hu and bool((win > hu).all()):
        side, lvl = "high", hu
    else:
        return None
    # Dedup keys are PRICE-based, not id-based: windowed regens renumber
    # range_ids as old records slide off the window's left edge, which would
    # reset id-based dedup and re-fire every recent alert.
    return {"tf": tf.upper(), "etype": "RANGE_BROKEN", "level_id": r["range_id"],
            "level_price": lvl, "dist": (lvl - price) / price, "strength": "RANGE",
            "src": r["range_id"], "ts": state.now.isoformat(),
            "last_event": (f"range_{side}", r["range_phase_2_ts"][:10]),
            "dedup_key": f"{tf}:range_{side}_{price_key(lvl)}:broken"}


def _confluence(touched):
    """Condition 6: cluster touched events within CONFLUENCE_WINDOW_H and
    CONFLUENCE_PROX; emit one alert per cluster of >= 2."""
    out, used = [], set()
    for i, a in enumerate(touched):
        if i in used:
            continue
        group, used_local = [a], {i}
        for j in range(i + 1, len(touched)):
            if j in used:
                continue
            b = touched[j]
            dt = abs(pd.Timestamp(a["ts"]) - pd.Timestamp(b["ts"]))
            dp = abs(a["level_price"] - b["level_price"]) / a["level_price"]
            if dt <= pd.Timedelta(hours=CONFLUENCE_WINDOW_H) and dp <= CONFLUENCE_PROX:
                group.append(b); used_local.add(j)
        if len(group) >= 2:
            used |= used_local
            tfs = "+".join(sorted({g["tf"] for g in group}))
            avg = sum(g["level_price"] for g in group) / len(group)
            out.append({"tf": tfs, "etype": "CONFLUENCE", "level_id": "confluence",
                        "level_price": avg, "dist": None, "strength": f"{len(group)}x",
                        "src": ",".join(sorted({g["src"] for g in group})),
                        "ts": max(g["ts"] for g in group), "last_event": None,
                        # 4 significant digits: the same ~0.01-0.1% bucket at
                        # every price scale (round(avg, 2) put every sub-cent
                        # cluster in the single bucket "0.0").
                        "dedup_key": f"confluence:{avg:.4g}"})
    return out


def _proximity_alerts(state, price, proximity_pct=PROXIMITY_THRESHOLD_PCT):
    """Proximity: live spot within proximity_pct (PERCENT) of a strong/weak level,
    even with NO touched/rejected/broken event. Warns BEFORE the structural event
    prints (e.g. a heads-up as price first approaches a retest level). Dedup'd 4h
    per level like everything else."""
    out = []
    thr = proximity_pct / 100.0           # percent -> fraction for the distance test
    for tf in TFS:
        for L in state._mem[tf]["levels"]:
            strg = L.get("strength_class")
            if strg not in ("strong", "weak"):
                continue
            d = (L["price"] - price) / price
            if abs(d) < thr:
                out.append({"tf": tf.upper(), "etype": "PROXIMITY", "level_id": L["level_id"],
                            "level_price": L["price"], "dist": d, "strength": strg,
                            "src": L["source_range_id"], "ts": state.now.isoformat(),
                            "last_event": (L.get("last_event_type"), L.get("last_event_ts")),
                            "dedup_key": f"proximity:{tf}:{price_key(L['price'])}"})
    return out


def scan(state, recency_h, proximity_pct=PROXIMITY_THRESHOLD_PCT):
    """Return the list of triggered alert dicts (pre-dedup). Recency is TF-aware:
    an event must be within max(recency_h, RECENCY_BARS * bar_hours[tf]) of the
    latest bar, so a 4H/1D event from its newest CLOSED bar still qualifies (a
    flat 4h floor would wrongly exclude every >= 4H event). Proximity uses the
    LIVE spot, not the last closed bar."""
    price = state.live_price
    alerts, touched = [], []
    for tf in TFS:
        cutoff = state.now - pd.Timedelta(hours=max(recency_h, RECENCY_BARS * BAR_HOURS[tf]))
        mem = state._mem[tf]
        strg_of = {L["level_id"]: L.get("strength_class") for L in mem["levels"]}
        last_of = {L["level_id"]: (L.get("last_event_type"), L.get("last_event_ts"))
                   for L in mem["levels"]}
        for e in mem["historical_level_events"]:
            if pd.Timestamp(e["ts"]) < cutoff:
                continue
            etype, lid, lp = e["type"], e["level_id"], e["level_price"]
            strg = strg_of.get(lid)
            dist = (lp - price) / price
            fire = (
                (etype == "historical_level_touched" and strg == "strong" and abs(dist) <= TOUCH_PROX) or
                (etype == "rejected" and strg in ("strong", "weak") and abs(dist) <= REJECT_PROX) or
                (etype == "broken" and strg == "strong") or
                (etype == "reclaimed" and strg == "strong")
            )
            if not fire:
                continue
            a = {"tf": tf.upper(), "etype": etype, "level_id": lid, "level_price": lp,
                 "dist": dist, "strength": strg, "src": e["source_range_id"],
                 "ts": e["ts"], "last_event": last_of.get(lid),
                 "dedup_key": f"{tf}:{price_key(lp)}:{etype}"}   # price-based: stable across window renumbering
            alerts.append(a)
            if etype == "historical_level_touched":
                touched.append(a)
    for tf in ("1d", "1w"):
        rb = _range_break_alert(state, tf)
        if rb:
            alerts.append(rb)
    alerts.extend(_confluence(touched))
    alerts.extend(_proximity_alerts(state, price, proximity_pct))
    return alerts


# ----------------------------------------------------------------- format
def _zone(bp):
    return bp["zone"] if bp else "n/a"


def _fmt_ts(ts):
    """YYYY-MM-DD HH:MM:SS UTC — full precision, no abbreviations."""
    return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_alert(a, price, state):
    pair = getattr(state, "pair", paths.DEFAULT_PAIR)
    # Prices via fmt_price (magnitude-aware): ':.2f' printed SHIB as "$0.00"
    # and two different DOGE levels both as "$0.09".
    ctx = (f"context: 1D={_zone(state.band_position('1d'))}, "
           f"1W={_zone(state.band_position('1w'))}  (live {fmt_price(state.live_price)} @ {_fmt_ts(state.live_ts)})")
    if a["etype"] == "PROXIMITY":
        sign = "+" if a["dist"] >= 0 else "-"
        line1 = (f"[{pair}] [PROXIMITY] price {fmt_price(price)} approaching {fmt_price(a['level_price'])} "
                 f"({sign}{abs(a['dist'])*100:.2f}%) | strength: {str(a['strength']).upper()} | src: {a['src']}")
        return line1 + "\n" + ctx
    ev = a["etype"].replace("historical_level_", "").upper()
    if a["dist"] is None:
        dist_str = "confluence"
    else:
        dist_str = f"{'+' if a['dist'] >= 0 else ''}{a['dist']*100:.1f}% from current {fmt_price(price)}"
    le = a["last_event"]
    last_str = f"{le[0]}@{le[1][:10]}" if le and le[0] else "none"
    line1 = (f"[{pair}] [{a['tf']}] {ev} level {fmt_price(a['level_price'])} ({dist_str}) | "
             f"strength: {str(a['strength']).upper()} | src: {a['src']} | last: {last_str}")
    return line1 + "\n" + ctx


# ----------------------------------------------------------------- dedup
def _load_state(pair=paths.DEFAULT_PAIR):
    sp = _STATE_PATH(pair)
    if sp.exists():
        try:
            return json.loads(sp.read_text())
        except Exception:
            return {}
    return {}


def _save_state(fired, pair=paths.DEFAULT_PAIR):
    sp = _STATE_PATH(pair)
    sp.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: same-dir temp + os.replace, so a concurrent reader (the API
    # volume is shared) never sees a half-written file.
    tmp = sp.with_name(f".{sp.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(fired, indent=2), encoding="utf-8")
    os.replace(tmp, sp)


def _should_fire(a, fired):
    prev = fired.get(a["dedup_key"])
    if prev is None:
        return True
    return (pd.Timestamp(a["ts"]) - pd.Timestamp(prev)) > pd.Timedelta(hours=DEDUP_HOURS)


# ----------------------------------------------------------------- run
def run_once(dry_run=False, recency_h=ALERT_RECENCY_HOURS,
             proximity_pct=PROXIMITY_THRESHOLD_PCT, fired=None, log=print,
             pair=paths.DEFAULT_PAIR, state=None):
    """One scan. Loads fresh State unless the caller passes a pre-built one
    (the service worker reuses its own; avoids double JSON loads + live fetch).
    Caller handles freshness/regen first. dry_run is a NON-DESTRUCTIVE preview:
    it dedups against the in-memory `fired` map but never writes
    alerts_state.json, so previews are repeatable and don't consume events from
    a later live run. Returns (n_new, fired)."""
    if state is None:
        from detectors.query_state import State
        state = State(pair)
    if fired is None:
        fired = _load_state(pair)
    candidates = sorted(scan(state, recency_h, proximity_pct), key=lambda a: a["ts"])
    n_new = n_failed = 0
    for a in candidates:
        if not _should_fire(a, fired):
            continue
        status = _deliver(format_alert(a, state.live_price, state), dry_run, log=log)
        if status == "failed":
            # NOT marked fired: a Telegram outage must not silently swallow
            # the alert — it is retried on the next scan while still recent.
            n_failed += 1
            continue
        fired[a["dedup_key"]] = a["ts"]
        n_new += 1
    if not dry_run:
        _save_state(fired, pair)
    price = state.live_price; live_ts = state.live_ts
    del state, candidates
    gc.collect()
    log(f"[alerts] scan complete: {n_new} new alert(s) at live {fmt_price(price)} "
        f"(fetched @ {_fmt_ts(live_ts)})"
        + (f"; {n_failed} NOT delivered (will retry next scan)" if n_failed else "")
        + ("  (dry-run: state NOT persisted)" if dry_run else ""))
    return n_new, fired


def watch(dry_run=False, interval_s=WATCH_INTERVAL_S, recency_h=ALERT_RECENCY_HOURS,
          proximity_pct=PROXIMITY_THRESHOLD_PCT, no_regen=False, log=print,
          pair=paths.DEFAULT_PAIR):
    from data.freshness_monitor import ensure_fresh
    log(f"[alerts] --watch every {interval_s//60}min (proximity +/-{proximity_pct}%). Ctrl+C to stop.")
    fired = _load_state(pair)                             # carried across iterations so dedup holds
    try:
        while True:
            ensure_fresh(regen=not no_regen, pair=pair, log=log)  # refresh + cascade-regen on new bars
            _, fired = run_once(dry_run=dry_run, recency_h=recency_h,
                                proximity_pct=proximity_pct, fired=fired, log=log,
                                pair=pair)
            gc.collect()
            time.sleep(interval_s)
    except KeyboardInterrupt:
        log("\n[alerts] stopped (Ctrl+C). clean exit.")


def main():
    p = argparse.ArgumentParser(description="Structural alerts (Telegram DM).")
    p.add_argument("--pair", default=paths.DEFAULT_PAIR)
    p.add_argument("--watch", action="store_true", help="Loop on --interval-seconds.")
    p.add_argument("--interval-seconds", type=int, default=WATCH_INTERVAL_S,
                   dest="interval_seconds", help="watch loop period in seconds (default 300).")
    p.add_argument("--recency-hours", type=int, default=ALERT_RECENCY_HOURS,
                   help="Only fire on events within the last N hours (default 4).")
    p.add_argument("--proximity-pct", type=float, default=PROXIMITY_THRESHOLD_PCT,
                   dest="proximity_pct", help="Proximity threshold in PERCENT (default 1.5).")
    p.add_argument("--dry-run", action="store_true", help="Print, don't send Telegram.")
    p.add_argument("--no-freshness", action="store_true", help="Skip data freshness check.")
    p.add_argument("--no-regen", action="store_true", help="Fetch but skip cascade regen.")
    p.add_argument("--test-telegram", action="store_true", help="Send one test DM and exit.")
    a = p.parse_args()

    _load_dotenv()
    from shared.memhygiene import install                 # Memory hygiene: see MEMORY_HYGIENE.md
    install("alerts")

    if a.test_telegram:
        ok = _telegram_send("✅ ttrronev alerts online — Telegram DM wired up.",
                            dry_run=a.dry_run)
        print(f"[alerts] telegram test {'sent' if ok else 'FAILED'}")
        return

    if not a.no_freshness and not a.watch:
        from data.freshness_monitor import ensure_fresh   # end-consumer: fetch + cascade regen
        ensure_fresh(regen=not a.no_regen, pair=a.pair)

    if a.watch:
        watch(dry_run=a.dry_run, interval_s=a.interval_seconds,
              recency_h=a.recency_hours, proximity_pct=a.proximity_pct,
              no_regen=a.no_regen, pair=a.pair)
    else:
        run_once(dry_run=a.dry_run, recency_h=a.recency_hours,
                 proximity_pct=a.proximity_pct, pair=a.pair)


if __name__ == "__main__":
    main()
