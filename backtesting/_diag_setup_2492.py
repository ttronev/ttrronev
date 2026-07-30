"""Diagnostic: trace engine state at BOS for setup 2492 (Apr 24 20:50 UTC).

Same approach as _diag_setup_2499 but for the LAST LOSER. Reproduces the
analyzer's per-bar trackers so we can show the engine-authoritative
swing_high / swing_low timestamps rather than a post-hoc CSV scan.
"""

from __future__ import annotations

import pandas as pd
import numpy as np

df = pd.read_csv("data/raw/SOL_USDT_5m.csv")
df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

end = pd.Timestamp("2026-04-26 06:00:00+00:00")
start = pd.Timestamp("2026-04-21 00:00:00+00:00")
sub = df[(df["timestamp"] >= start) & (df["timestamp"] <= end)].reset_index(drop=True)
print(f"analyzer input: {len(sub):,} bars from {sub.timestamp.iloc[0]} to {sub.timestamp.iloc[-1]}")


def trace_analyzer(df_in: pd.DataFrame, reversal_pct: float = 0.005,
                   init_bars: int = 50):
    n = len(df_in)
    high = df_in["high"].to_numpy(dtype=float)
    low = df_in["low"].to_numpy(dtype=float)
    close = df_in["close"].to_numpy(dtype=float)
    ts = df_in["timestamp"].to_numpy()

    init_n = min(init_bars, n)
    locked_sH = float(np.max(high[:init_n])); locked_sH_idx = int(np.argmax(high[:init_n]))
    locked_sL = float(np.min(low[:init_n])); locked_sL_idx = int(np.argmin(low[:init_n]))
    state = "undetermined"

    zz_dir = 0
    active_high_price = float(high[0]); active_high_idx = 0
    active_low_price = float(low[0]); active_low_idx = 0
    tracking_LL = float("inf"); tracking_HH = float("-inf")
    highest_H_price = float("-inf"); highest_H_idx = -1
    lowest_L_price = float("inf"); lowest_L_idx = -1
    latest_H_price = float("nan"); latest_H_idx = -1
    latest_L_price = float("nan"); latest_L_idx = -1
    sH_ratchet_pending = False; sL_ratchet_pending = False

    snapshots, pivots = [], []

    for i in range(n):
        bh, bl, bc = float(high[i]), float(low[i]), float(close[i])

        if zz_dir in (0, +1) and bh > active_high_price:
            active_high_price = bh; active_high_idx = i
        if zz_dir in (0, -1) and bl < active_low_price:
            active_low_price = bl; active_low_idx = i

        new_pivot = None
        if zz_dir in (0, +1) and active_high_price > 0 and bc < active_high_price * (1.0 - reversal_pct):
            new_pivot = ("H", active_high_idx, active_high_price)
            active_low_price = bl; active_low_idx = i; zz_dir = -1
        if new_pivot is None and zz_dir in (0, -1) and active_low_price > 0 and bc > active_low_price * (1.0 + reversal_pct):
            new_pivot = ("L", active_low_idx, active_low_price)
            active_high_price = bh; active_high_idx = i; zz_dir = +1

        ratcheted = None
        if new_pivot is not None:
            kind, p_idx, p_price = new_pivot
            pivots.append((kind, p_price, str(ts[p_idx]), str(ts[i])))
            if kind == "H":
                if p_price > highest_H_price:
                    highest_H_price = p_price; highest_H_idx = p_idx
                latest_H_price = p_price; latest_H_idx = p_idx
                if sH_ratchet_pending and p_price > locked_sH:
                    locked_sH = p_price; locked_sH_idx = p_idx
                    ratcheted = ("sH", p_price, p_idx)
                if sH_ratchet_pending: sH_ratchet_pending = False
            else:
                if p_price < lowest_L_price:
                    lowest_L_price = p_price; lowest_L_idx = p_idx
                latest_L_price = p_price; latest_L_idx = p_idx
                if sL_ratchet_pending and p_price < locked_sL:
                    locked_sL = p_price; locked_sL_idx = p_idx
                    ratcheted = ("sL", p_price, p_idx)
                if sL_ratchet_pending: sL_ratchet_pending = False

        if state in ("downtrend", "undetermined") and bl < tracking_LL:
            tracking_LL = bl
        if state in ("uptrend", "undetermined") and bh > tracking_HH:
            tracking_HH = bh

        prev = {
            "state": state,
            "locked_sH": locked_sH, "locked_sH_idx": locked_sH_idx,
            "locked_sL": locked_sL, "locked_sL_idx": locked_sL_idx,
            "highest_H": highest_H_price, "highest_H_idx": highest_H_idx,
            "latest_H": latest_H_price, "latest_H_idx": latest_H_idx,
            "lowest_L": lowest_L_price, "lowest_L_idx": lowest_L_idx,
            "latest_L": latest_L_price, "latest_L_idx": latest_L_idx,
            "sH_ratchet_pending": sH_ratchet_pending,
            "sL_ratchet_pending": sL_ratchet_pending,
        }

        event = "none"; carryover = None
        if bc > locked_sH and not sH_ratchet_pending:
            if state == "downtrend":
                new_sL = latest_L_price if not np.isnan(latest_L_price) else bl
                new_sL_idx = latest_L_idx if latest_L_idx >= 0 else i
                state = "uptrend"; carryover = "flip_up: latest_L"
            elif state == "uptrend":
                new_sL = lowest_L_price if lowest_L_price != float("inf") else locked_sL
                new_sL_idx = lowest_L_idx if lowest_L_idx >= 0 else locked_sL_idx
                carryover = "continuation_up: lowest_L"
            else:
                new_sL = locked_sL; new_sL_idx = locked_sL_idx; state = "uptrend"
                carryover = "undet_up"
            locked_sH = bc; locked_sH_idx = i
            locked_sL = new_sL; locked_sL_idx = new_sL_idx
            tracking_LL = float("inf"); tracking_HH = bh
            highest_H_price = float("-inf"); highest_H_idx = -1
            lowest_L_price = float("inf"); lowest_L_idx = -1
            latest_H_price = float("nan"); latest_H_idx = -1
            latest_L_price = float("nan"); latest_L_idx = -1
            sH_ratchet_pending = True; sL_ratchet_pending = False
            event = "bos_up"
        elif bc < locked_sL and not sL_ratchet_pending:
            if state == "uptrend":
                new_sH = latest_H_price if not np.isnan(latest_H_price) else bh
                new_sH_idx = latest_H_idx if latest_H_idx >= 0 else i
                state = "downtrend"; carryover = "flip_down: latest_H_in_swing"
            elif state == "downtrend":
                new_sH = highest_H_price if highest_H_price != float("-inf") else locked_sH
                new_sH_idx = highest_H_idx if highest_H_idx >= 0 else locked_sH_idx
                carryover = "continuation_down: highest_H_in_swing"
            else:
                new_sH = locked_sH; new_sH_idx = locked_sH_idx; state = "downtrend"
                carryover = "undet_down"
            locked_sL = bc; locked_sL_idx = i
            locked_sH = new_sH; locked_sH_idx = new_sH_idx
            tracking_LL = bl; tracking_HH = float("-inf")
            highest_H_price = float("-inf"); highest_H_idx = -1
            lowest_L_price = float("inf"); lowest_L_idx = -1
            latest_H_price = float("nan"); latest_H_idx = -1
            latest_L_price = float("nan"); latest_L_idx = -1
            sL_ratchet_pending = True; sH_ratchet_pending = False
            event = "bos_down"

        snapshots.append({
            "i": i, "ts": ts[i], "event": event, "carryover": carryover,
            "high": bh, "low": bl, "close": bc, "pre_bos": prev,
            "ratcheted": ratcheted,
            "post_locked_sH": locked_sH, "post_locked_sH_idx": locked_sH_idx,
            "post_locked_sL": locked_sL, "post_locked_sL_idx": locked_sL_idx,
            "post_state": state,
        })

    return snapshots, pivots


snaps, pivots = trace_analyzer(sub)
ts_arr = sub["timestamp"].to_numpy()


def fmt_ts(idx):
    if idx is None or idx < 0:
        return "NA"
    return str(pd.Timestamp(ts_arr[idx]))


def fmt_val(v):
    if v is None or (isinstance(v, float) and (np.isinf(v) or np.isnan(v))):
        return "none"
    return f"{v:.4f}"


target_ts = pd.Timestamp("2026-04-24 20:50:00+00:00")
bos = next((s for s in snaps if pd.Timestamp(s["ts"]) == target_ts), None)

print()
print("=" * 88)
print("ENGINE STATE AT BOS BAR — Apr 24 20:50:00 UTC (setup 2492)")
print("=" * 88)
if bos is None:
    print("NO BOS at 20:50!")
else:
    pre = bos["pre_bos"]
    print(f"  bar OHLC:    high={bos['high']:.4f}  low={bos['low']:.4f}  close={bos['close']:.4f}")
    print(f"  state BEFORE BOS: {pre['state']}")
    print(f"  event fired:      {bos['event']}")
    print(f"  carryover rule:   {bos['carryover']}")
    print()
    print("  PRE-BOS locked levels (the levels being broken):")
    print(f"    locked_sH = {fmt_val(pre['locked_sH'])} @ {fmt_ts(pre['locked_sH_idx'])}")
    print(f"    locked_sL = {fmt_val(pre['locked_sL'])} @ {fmt_ts(pre['locked_sL_idx'])}")
    print()
    print("  PRE-BOS per-swing pivot trackers:")
    print(f"    highest_H_in_swing = {fmt_val(pre['highest_H'])} @ {fmt_ts(pre['highest_H_idx'])}")
    print(f"    latest_H_in_swing  = {fmt_val(pre['latest_H'])} @ {fmt_ts(pre['latest_H_idx'])}")
    print(f"    lowest_L_in_swing  = {fmt_val(pre['lowest_L'])} @ {fmt_ts(pre['lowest_L_idx'])}")
    print(f"    latest_L_in_swing  = {fmt_val(pre['latest_L'])} @ {fmt_ts(pre['latest_L_idx'])}")
    print(f"    sH_ratchet_pending={pre['sH_ratchet_pending']}  sL_ratchet_pending={pre['sL_ratchet_pending']}")
    print()
    print(f"  Trigger close: {bos['close']:.4f}  broke locked_sH = {fmt_val(pre['locked_sH'])}")
    print()
    print("  POST-BOS new locked levels (what becomes the trade's swing):")
    print(f"    new locked_sH = {bos['post_locked_sH']:.4f} @ {fmt_ts(bos['post_locked_sH_idx'])}  (provisional, will ratchet)")
    print(f"    new locked_sL = {bos['post_locked_sL']:.4f} @ {fmt_ts(bos['post_locked_sL_idx'])}  <-- the swing_low reference")

# After BOS, find the ratchet event that locks sH
print()
print("=" * 88)
print("POST-BOS RATCHET (finalizes swing_high)")
print("=" * 88)
post_window_end = pd.Timestamp("2026-04-25 22:00:00+00:00")
for s in snaps:
    sts = pd.Timestamp(s["ts"])
    if sts <= target_ts:
        continue
    if sts > post_window_end:
        break
    if s["ratcheted"] is not None:
        kind, p, p_idx = s["ratcheted"]
        print(f"  Ratchet @ {s['ts']}: locked_{kind} ratcheted to {p:.4f} (pivot @ {fmt_ts(p_idx)})")
        break

print()
print("=" * 88)
print("BOS / EVENT HISTORY: Apr 23 12:00 -> Apr 26 06:00 UTC")
print("=" * 88)
print(f"{'idx':>5} {'ts':<25} {'event':<10} {'st_after':<11} {'close':>8} "
      f"{'new_sH':>10} ({'sH_ts':<5}) {'new_sL':>10} ({'sL_ts':<5})  carryover")
window_start = pd.Timestamp("2026-04-23 12:00:00+00:00")
window_end = pd.Timestamp("2026-04-26 06:00:00+00:00")
for s in snaps:
    sts = pd.Timestamp(s["ts"])
    if not (window_start <= sts <= window_end):
        continue
    if s["event"] == "none":
        continue
    sH_t = fmt_ts(s["post_locked_sH_idx"])[5:16]
    sL_t = fmt_ts(s["post_locked_sL_idx"])[5:16]
    print(f"{s['i']:>5} {str(s['ts']):<25} {s['event']:<10} {s['post_state']:<11} "
          f"{s['close']:>8.4f} {s['post_locked_sH']:>10.4f} ({sH_t}) {s['post_locked_sL']:>10.4f} ({sL_t})  "
          f"{s['carryover'] or ''}")

print()
print("=" * 88)
print("ALL CONFIRMED PIVOTS Apr 23 12:00 -> Apr 25 22:00 UTC")
print("=" * 88)
print(f"{'kind':<5} {'price':>9} {'pivot_at_ts':<25} {'confirmed_when_close_at':<25}")
for kind, p_price, p_ts, conf_ts in pivots:
    pts = pd.Timestamp(p_ts)
    if not (window_start <= pts <= pd.Timestamp("2026-04-25 22:00:00+00:00")):
        continue
    print(f"{kind:<5} {p_price:>9.4f} {p_ts:<25} {conf_ts:<25}")
