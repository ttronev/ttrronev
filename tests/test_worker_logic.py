"""Worker / regen decision logic: what a restart regenerates, which timeframes
a structural cycle checks, when a pair is quarantined. Network, detectors and
Telegram are stubbed — these test decisions, not detection."""
import json
import os

import pandas as pd
import pytest

from detectors import paths
from service import regen, worker as worker_mod
from service.pairs import TF_MS, WORKER_TFS, MEMORY_TFS
from tests.synth import synth_1h

PAIR = "W_USDT"
H = TF_MS["1h"]
DAY = TF_MS["1d"]


# --------------------------------------------------------------- fingerprint
def test_fingerprint_is_stable_and_line_ending_insensitive(tmp_path):
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\n")
    (tmp_path / "b.py").write_bytes(b"z = 3\n")
    fp = regen.code_fingerprint(files=["a.py", "b.py"], root=tmp_path)
    assert fp == regen.code_fingerprint(files=["b.py", "a.py"], root=tmp_path)
    (tmp_path / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")          # Windows checkout
    assert regen.code_fingerprint(files=["a.py", "b.py"], root=tmp_path) == fp


def test_fingerprint_changes_when_detection_code_changes(tmp_path):
    (tmp_path / "a.py").write_bytes(b"THRESHOLD = 0.003\n")
    before = regen.code_fingerprint(files=["a.py"], root=tmp_path)
    (tmp_path / "a.py").write_bytes(b"THRESHOLD = 0.004\n")
    assert regen.code_fingerprint(files=["a.py"], root=tmp_path) != before


def test_fingerprint_covers_every_file_that_shapes_artifacts():
    for rel in regen._FINGERPRINT_FILES:
        assert (paths.ROOT / rel).exists(), rel
    must = {"detectors/range_detector_core.py", "detectors/layer5_range_memory.py",
            "detectors/layer5_1_strength.py", "shared/pricefmt.py", "service/pairs.py"}
    assert must <= set(regen._FINGERPRINT_FILES)
    assert len(regen.code_fingerprint()) == 16


# ------------------------------------------------------ staleness / catch-up
def _touch(p, mtime):
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("{}", encoding="utf-8")
    os.utime(p, (mtime, mtime))


def _fake_artifacts(pair=PAIR, csv_mtime=1_000_000, art_mtime=1_000_100):
    """CSV + layer-1 (+ memory) files for all six timeframes, artifacts newer."""
    for tf in WORKER_TFS:
        _touch(paths.raw_csv(tf, pair), csv_mtime)
        _touch(paths.l1_json(tf, pair), art_mtime)
    for tf in MEMORY_TFS:
        _touch(paths.mem_json(tf, pair), art_mtime)


def test_l1_is_stale(sandbox):
    assert regen.l1_is_stale(PAIR, "1h")                    # nothing on disk
    _fake_artifacts()
    assert not regen.l1_is_stale(PAIR, "1h")                # artifact newer than CSV
    _touch(paths.raw_csv("1h", PAIR), 1_000_200)            # candles appended since
    assert regen.l1_is_stale(PAIR, "1h")
    assert not regen.l1_is_stale(PAIR, "4h")


def _write_csv(tf, last_open_ms, pair=PAIR, bars=5):
    step = TF_MS[tf]
    ts = [last_open_ms - step * i for i in range(bars)][::-1]
    df = pd.DataFrame({"timestamp": ts, "open": 1.0, "high": 1.0, "low": 1.0,
                       "close": 1.0, "volume": 1.0})
    p = paths.raw_csv(tf, pair)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)


def test_behind_tfs_flags_only_timeframes_missing_their_last_closed_bar(sandbox):
    b = 20_000 * DAY + 13 * H                               # a 13:00 UTC boundary
    _write_csv("1h", b - H)                                 # has the 12:00 bar: current
    _write_csv("2h", b - H - 2 * H)                         # last closed 2h opened 10:00; has it
    _write_csv("4h", (b // TF_MS["4h"]) * TF_MS["4h"] - 2 * TF_MS["4h"])   # one bar short
    _write_csv("1d", (b // DAY) * DAY - DAY)                # yesterday's bar: current
    assert regen.behind_tfs(PAIR, b) == ["4h"]


def test_behind_tfs_after_a_missed_daily_close(sandbox):
    b = 20_000 * DAY + 3 * H                                # 03:00, daily close was missed
    _write_csv("1h", b - H)
    _write_csv("2h", (b // TF_MS["2h"]) * TF_MS["2h"] - TF_MS["2h"])
    _write_csv("4h", (b // TF_MS["4h"]) * TF_MS["4h"] - TF_MS["4h"])
    _write_csv("1d", (b // DAY) * DAY - 2 * DAY)            # still two days back
    assert regen.behind_tfs(PAIR, b) == ["1d"]


def test_behind_tfs_ignores_missing_csv(sandbox):
    assert regen.behind_tfs(PAIR, 20_000 * DAY) == []


def test_clock_due_timeframes():
    f = worker_mod._due_structural_tfs
    assert f(20_000 * DAY) == ["1w", "1d", "4h", "2h", "1h"]
    assert f(20_000 * DAY + 1 * H) == ["1h"]
    assert f(20_000 * DAY + 2 * H) == ["2h", "1h"]
    assert f(20_000 * DAY + 4 * H) == ["4h", "2h", "1h"]


# --------------------------------------------------------------- window slice
def test_window_slice_equals_a_whole_file_read(sandbox):
    df = synth_1h(cycles=12)                                # ~65 days of 1h bars
    p = paths.raw_csv("1h", PAIR)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)
    days = 20
    got = pd.read_csv(regen._window_slice(PAIR, "1h", days, "t"))
    full = pd.read_csv(p)
    want = full[full["timestamp"] >= int(full["timestamp"].max()) - days * regen.DAY_MS]
    pd.testing.assert_frame_equal(got, want.reset_index(drop=True), check_dtype=False)
    assert len(got) == days * 24 + 1


# ------------------------------------------------------------------- startup
@pytest.fixture
def wk(sandbox, monkeypatch):
    """A Worker with fetch / detector / chain / state stubbed and recorded."""
    calls = {"fetch": [], "regen": [], "chain": 0, "state": 0}
    fetched = {}                                             # tf -> rows (or Exception)

    def fake_fetch(pair, tf, log=print):
        calls["fetch"].append(tf)
        v = fetched.get(tf, 0)
        if isinstance(v, Exception):
            raise v
        return v

    def fake_regen(pair, tf, log=print):
        calls["regen"].append(tf)
        _touch(paths.l1_json(tf, pair), 2_000_000)           # artifact now fresh
        return 0.0

    def fake_chain(pair, log=print):
        calls["chain"] += 1
        for tf in MEMORY_TFS:
            _touch(paths.mem_json(tf, pair), 2_000_000)
        return 0.0

    monkeypatch.setattr(regen, "fetch_tf", fake_fetch)
    monkeypatch.setattr(regen, "regen_detector", fake_regen)
    monkeypatch.setattr(regen, "regen_chain", fake_chain)
    monkeypatch.setattr(regen, "_fingerprint_cache", "fp-current")
    w = worker_mod.Worker()
    monkeypatch.setattr(w, "_rebuild_state_and_alerts",
                        lambda pair, seed_alerts=False: calls.__setitem__("state", calls["state"] + 1))
    monkeypatch.setattr(w, "report_error", lambda key, exc: calls.setdefault("errors", []).append(key))
    yield w, calls, fetched
    w.regen_pool.shutdown(wait=False)
    w.live_pool.shutdown(wait=False)


def _stamped(pair=PAIR):
    return sorted(json.loads(paths.heartbeat_json(pair).read_text())["tfs"])


def test_first_start_regenerates_everything_and_writes_the_marker(wk):
    w, calls, _ = wk
    _fake_artifacts()                                        # artifacts exist, but no marker
    w._startup_sync(PAIR)
    assert calls["regen"] == ["1w", "1d", "4h", "2h", "1h", "5m"]
    assert calls["chain"] == 1 and calls["state"] == 1
    assert regen.read_marker(PAIR)["fingerprint"] == "fp-current"


def test_restart_with_nothing_new_regenerates_nothing(wk):
    """The ~15-minute restart: every pair used to be fully regenerated."""
    w, calls, _ = wk
    _fake_artifacts()
    regen.write_marker(PAIR)
    w._startup_sync(PAIR)
    assert calls["fetch"] == ["1w", "1d", "4h", "2h", "1h", "5m"]      # still checks for new bars
    assert calls["regen"] == [] and calls["chain"] == 0
    assert calls["state"] == 1                                         # state.json refreshed
    assert _stamped() == sorted(WORKER_TFS)


def test_restart_regenerates_only_timeframes_with_new_bars(wk):
    w, calls, fetched = wk
    _fake_artifacts()
    regen.write_marker(PAIR)
    fetched.update({"1h": 3, "5m": 40})
    w._startup_sync(PAIR)
    assert calls["regen"] == ["1h", "5m"]
    assert calls["chain"] == 1                               # 1h is a memory timeframe


def test_new_5m_bars_alone_do_not_rerun_the_chain(wk):
    w, calls, fetched = wk
    _fake_artifacts()
    regen.write_marker(PAIR)
    fetched["5m"] = 12
    w._startup_sync(PAIR)
    assert calls["regen"] == ["5m"] and calls["chain"] == 0


def test_changed_detection_code_forces_one_full_regen(wk, monkeypatch):
    w, calls, _ = wk
    _fake_artifacts()
    regen.write_marker(PAIR)                                 # written by "fp-current"
    monkeypatch.setattr(regen, "_fingerprint_cache", "fp-after-deploy")
    w._startup_sync(PAIR)
    assert calls["regen"] == ["1w", "1d", "4h", "2h", "1h", "5m"] and calls["chain"] == 1
    assert regen.read_marker(PAIR)["fingerprint"] == "fp-after-deploy"
    calls["regen"].clear(); calls["chain"] = 0
    w._startup_sync(PAIR)                                    # next restart: quiet again
    assert calls["regen"] == [] and calls["chain"] == 0


def test_missing_or_outdated_artifact_is_regenerated(wk):
    w, calls, _ = wk
    _fake_artifacts()
    regen.write_marker(PAIR)
    paths.l1_json("4h", PAIR).unlink()                       # lost artifact
    _touch(paths.raw_csv("2h", PAIR), 1_000_500)             # candles newer than artifact
    w._startup_sync(PAIR)
    assert calls["regen"] == ["4h", "2h"] and calls["chain"] == 1


def test_missing_memory_file_reruns_the_chain(wk):
    w, calls, _ = wk
    _fake_artifacts()
    regen.write_marker(PAIR)
    paths.mem_json("1d", PAIR).unlink()
    w._startup_sync(PAIR)
    assert calls["regen"] == [] and calls["chain"] == 1


def test_failed_fetch_is_reported_and_not_stamped_as_fresh(wk):
    w, calls, fetched = wk
    _fake_artifacts()
    regen.write_marker(PAIR)
    fetched["5m"] = RuntimeError("OKX request failed")
    w._startup_sync(PAIR)
    assert calls["errors"] == [f"{PAIR}:5m:startup-fetch"]
    assert "5m" not in _stamped() and "1h" in _stamped()


def test_a_pair_joins_the_cycles_as_soon_as_its_own_startup_is_done(wk, monkeypatch):
    """The loops run from process start but only serve started pairs — so
    the first pair finished is kept fresh while the others are still
    catching up, instead of going stale behind a 20-minute startup."""
    w, calls, _ = wk
    monkeypatch.setattr(worker_mod.registry, "ready_pairs", lambda: [PAIR, "LATER_USDT"])
    assert w._ready_pairs() == [PAIR, "LATER_USDT"]     # live price: everyone
    assert w._my_pairs() == []                          # cycles: nobody yet
    _fake_artifacts()
    w._startup_sync(PAIR)
    assert w._my_pairs() == [PAIR]                      # first pair joins alone
    assert "LATER_USDT" not in w.started


# ---------------------------------------------------------- structural cycle
def test_structural_cycle_catches_up_a_timeframe_that_is_behind(wk, monkeypatch):
    w, calls, fetched = wk
    _fake_artifacts()
    monkeypatch.setattr(regen, "behind_tfs", lambda pair, b: ["1d"])
    monkeypatch.setattr(worker_mod, "FETCH_RETRY_DELAY_S", 0)
    fetched.update({"1d": 1, "1h": 1})
    w._structural_cycle_sync(PAIR, 20_000 * DAY + 3 * H)     # clock says only 1h is due
    assert calls["fetch"] == ["1d", "1h"]
    assert calls["regen"] == ["1d", "1h"] and calls["chain"] == 1


def test_structural_cycle_without_gaps_checks_only_clock_due(wk, monkeypatch):
    w, calls, fetched = wk
    _fake_artifacts()
    monkeypatch.setattr(regen, "behind_tfs", lambda pair, b: [])
    monkeypatch.setattr(worker_mod, "FETCH_RETRY_DELAY_S", 0)
    fetched["1h"] = 1
    w._structural_cycle_sync(PAIR, 20_000 * DAY + 3 * H)
    assert calls["fetch"] == ["1h"] and calls["regen"] == ["1h"]


# ---------------------------------------------------------------- quarantine
@pytest.fixture
def qw(monkeypatch):
    w = worker_mod.Worker()
    updates, notices = [], []
    monkeypatch.setattr(worker_mod.registry, "update_entry",
                        lambda pair, **f: updates.append((pair, f)))
    monkeypatch.setattr(w.live_pool, "submit", lambda fn, *a: notices.append(a[0]))
    yield w, updates, notices
    w.regen_pool.shutdown(wait=False)
    w.live_pool.shutdown(wait=False)


def test_a_pair_that_keeps_failing_alone_is_quarantined(qw):
    w, updates, notices = qw
    err = RuntimeError("OKX error: instrument does not exist")
    for i in range(worker_mod.QUARANTINE_AFTER - 1):
        assert w._update_quarantine(["SOL_USDT", "BTC_USDT"], [("DEAD_USDT", err)]) == []
    assert updates == []
    assert w._update_quarantine(["SOL_USDT"], [("DEAD_USDT", err)]) == ["DEAD_USDT"]
    pair, fields = updates[0]
    assert pair == "DEAD_USDT" and fields["status"] == worker_mod.registry.ERROR
    assert "instrument does not exist" in fields["error_reason"]
    assert len(notices) == 1 and "DEAD_USDT" in notices[0]
    assert "DEAD_USDT" not in w.fail_streak


def test_exchange_outage_never_quarantines_anyone(qw):
    """Every pair failing = systemic (OKX/network down). An hour of that must
    not move the whole universe to status=error."""
    w, updates, _ = qw
    err = RuntimeError("OKX request failed after 3 attempts")
    everyone = [("SOL_USDT", err), ("BTC_USDT", err), ("ETH_USDT", err)]
    for _ in range(worker_mod.QUARANTINE_AFTER * 3):
        assert w._update_quarantine([], everyone) == []
    assert updates == [] and w.fail_streak == {}


def test_a_success_resets_the_streak(qw):
    w, updates, _ = qw
    err = RuntimeError("flaky")
    for _ in range(worker_mod.QUARANTINE_AFTER - 1):
        w._update_quarantine(["SOL_USDT"], [("FLAKY_USDT", err)])
    w._update_quarantine(["SOL_USDT", "FLAKY_USDT"], [])
    for _ in range(worker_mod.QUARANTINE_AFTER - 1):
        w._update_quarantine(["SOL_USDT"], [("FLAKY_USDT", err)])
    assert updates == []
