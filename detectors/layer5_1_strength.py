"""
detectors/layer5_1_strength.py — Layer 5.1 (strong / weak / neutralized).

CLASSIFICATION layer on top of the locked layer-5 v1 raw event stream. No
new event logic: reads each level's summary rollups from
range_memory_<tf>.json plus the SOURCE range's cleanness_metrics from the
layer-1 json, then assigns every level a numeric strength_score in [0,1]
and a strength_class. The two new fields (plus a strength_factors
breakdown) are written back onto each level in range_memory_<tf>.json.

Three classes (per the spec). cycle_count GATES the class — chop is the
headline signal, so a level that price keeps slicing through cannot be
"strong" however pristine its birth range was:
    strong       cycle_count <= STRONG_MAX_CYCLES AND score >= STRONG_SCORE.
                 A level that has held — minimal chop, rejection-heavy, born
                 from a clean long structural range. Prioritise.
    weak         below the neutralized cycle count but not strong — chopped
                 a few times (cycle_count in the middle band), break-heavy,
                 or born from a short/scrappy range, or simply mid-score.
    neutralized  a level price has crossed so many times it has effectively
                 become noise. HARD RULE: cycle_count >= NEUTRALIZED_CYCLES
                 forces this class regardless of score (flag, deprioritise).

strength_score is a weighted blend of five normalised [0,1] factors:
    chop   1 - cycle_count/NEUTRALIZED_CYCLES   (fewer round trips = stronger)
    hold   (rejections + 0.5*touches) / (that + breaks)   (did it hold when
           tested? rejections — sweeps that failed — count fully, gentle
           touches half, breaks against it; no interactions => neutral 0.5)
    clean  (source time_inside_band_pct - 85) / 15        (cleanness above
           the 85% gate, saturating at 100%)
    dur    min(1, source active_duration_bars / DUR_SAT)  (longer source =
           more structural; bars already normalise per-TF)
    tf     structural weight of the source TF (1W/1D = 1.0 ... 1H = 0.3)

The hold factor encodes BOTH heuristics the spec names: a high
rejection-to-touch mix lifts it (strong) and a break-favoring mix sinks it
(weak). Weights/thresholds are module constants so they stay easy to tune.

v1-locked dependency: requires the layer-5 summary fields (cycle_count,
total_*). Run layer5_range_memory.py first.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detectors import paths
from shared.ioutil import atomic_write_json      # never leave a truncated artifact

TFS = ["1w", "1d", "4h", "2h", "1h"]

NEUTRALIZED_CYCLES = 5          # hard rule: >= this many cycles => neutralized
STRONG_MAX_CYCLES = 1          # strong requires cycle_count <= this (chop gate).
                               # Locked at 1 per spec: a level crossed more than
                               # once is no longer "strong" however clean its
                               # birth range was.
STRONG_SCORE = 0.55            # AND score at/above this => strong (else weak)
DUR_SAT = 80                   # source bars at which dur factor saturates
CLEAN_GATE = 85.0              # the layer-5 cleanness floor (normalise above it)

WEIGHTS = {"chop": 0.35, "hold": 0.30, "clean": 0.15, "dur": 0.10, "tf": 0.10}
TF_WEIGHT = {"1w": 1.0, "1d": 1.0, "4h": 0.6, "2h": 0.4, "1h": 0.3}


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _factors(level: dict, src_cm: dict, tf: str) -> dict:
    cyc = level["cycle_count"]
    touches = level["total_touches"]
    rej = level["total_rejections"]
    brk = level["total_breaks"]

    chop = _clamp01(1.0 - cyc / NEUTRALIZED_CYCLES)

    held = rej + 0.5 * touches
    denom = held + brk
    hold = (held / denom) if denom > 0 else 0.5

    tip = src_cm.get("time_inside_band_pct") if src_cm else None
    clean = _clamp01(((tip - CLEAN_GATE) / (100.0 - CLEAN_GATE))) if tip is not None else 0.0

    dur_bars = src_cm.get("active_duration_bars") if src_cm else None
    dur = _clamp01((dur_bars / DUR_SAT)) if dur_bars is not None else 0.0

    tf_f = TF_WEIGHT.get(tf, 0.3)

    return {"chop": round(chop, 4), "hold": round(hold, 4),
            "clean": round(clean, 4), "dur": round(dur, 4), "tf": round(tf_f, 4)}


def _score(f: dict) -> float:
    return sum(WEIGHTS[k] * f[k] for k in WEIGHTS)


def classify_tf(tf: str, pair: str = paths.DEFAULT_PAIR) -> dict:
    mem = json.loads(paths.mem_json(tf, pair).read_text())
    src = {r["range_id"]: r for r in json.loads(paths.l1_json(tf, pair).read_text())["ranges"]}

    dist = {"strong": 0, "weak": 0, "neutralized": 0}
    for lvl in mem["levels"]:
        src_cm = src.get(lvl["source_range_id"], {}).get("cleanness_metrics", {})
        f = _factors(lvl, src_cm, tf)
        score = _score(f)
        if lvl["cycle_count"] >= NEUTRALIZED_CYCLES:
            cls = "neutralized"
        elif lvl["cycle_count"] <= STRONG_MAX_CYCLES and score >= STRONG_SCORE:
            cls = "strong"
        else:
            cls = "weak"
        lvl["strength_score"] = round(score, 4)
        lvl["strength_class"] = cls
        lvl["strength_factors"] = f
        dist[cls] += 1

    mem["layer5_1"] = {
        "version": 1,
        "params": {
            "neutralized_cycles": NEUTRALIZED_CYCLES,
            "strong_max_cycles": STRONG_MAX_CYCLES, "strong_score": STRONG_SCORE,
            "dur_saturation_bars": DUR_SAT, "clean_gate": CLEAN_GATE,
            "weights": WEIGHTS, "tf_weight": TF_WEIGHT,
        },
        "class_distribution": dist,
    }
    atomic_write_json(paths.mem_json(tf, pair), mem)
    return {"tf": tf, "n_levels": len(mem["levels"]), "dist": dist}


def run(tfs=None, verbose=True, pair=paths.DEFAULT_PAIR):
    tfs = tfs or TFS
    out = {}
    for tf in tfs:
        s = classify_tf(tf, pair)
        out[tf] = s
        if verbose:
            d = s["dist"]
            print(f"[strength] {tf}: {s['n_levels']} levels  "
                  f"strong={d['strong']} weak={d['weak']} neutralized={d['neutralized']}")
    return out


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("layer5_1_strength")
    from data.freshness_monitor import consumer_startup   # Freshness contract: see MEMORY_HYGIENE.md
    consumer_startup(end_consumer=False)                   # CSV refresh only (regen-chain step; --no-freshness to skip)
    run()
