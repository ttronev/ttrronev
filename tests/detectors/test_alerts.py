"""detectors/alerts.py — delivery bookkeeping, dedup, and scale-safe text.

No network: _deliver is replaced by a stub in every test that would send."""
import pandas as pd
import pytest

from detectors import alerts
from tests.synth import SHIB_SCALE

NOW = pd.Timestamp("2026-08-28T15:00:00+00:00")


class FakeState:
    """The minimum of query_state.State that alerts.scan / format_alert use."""

    def __init__(self, price, levels, events=(), pair="TEST_USDT"):
        self.pair = pair
        self.price = self.live_price = price
        self.live_ok = True
        self.now = NOW
        self.live_ts = NOW
        self._l1 = {tf: {"config": {"recovery_lookahead": 12}, "ranges": []}
                    for tf in alerts.TFS}
        self._mem = {tf: {"levels": [], "historical_level_events": []}
                     for tf in alerts.TFS}
        self._mem["1h"]["levels"] = list(levels)
        self._mem["1h"]["historical_level_events"] = list(events)

    def active_range(self, tf):
        return None

    def band_position(self, tf):
        return None


def _lvl(price, cls="strong", lid="R001_1h_up_20260801_HI"):
    return {"level_id": lid, "price": price, "strength_class": cls,
            "source_range_id": lid[:-3], "last_event_type": None, "last_event_ts": None}


@pytest.fixture
def near_state():
    return FakeState(100.0, [_lvl(100.5)])       # one strong level 0.5% away


def _stub_deliver(monkeypatch, status):
    sent = []

    def fake(text, dry_run, log=print):
        sent.append(text)
        return status
    monkeypatch.setattr(alerts, "_deliver", fake)
    return sent


def test_proximity_candidate_is_found(near_state):
    cands = alerts.scan(near_state, alerts.ALERT_RECENCY_HOURS)
    assert [(a["etype"], a["level_price"]) for a in cands] == [("PROXIMITY", 100.5)]


def test_sent_alert_is_marked_fired(sandbox, monkeypatch, near_state):
    sent = _stub_deliver(monkeypatch, "sent")
    n, fired = alerts.run_once(state=near_state, fired={}, log=lambda m: None,
                               pair=near_state.pair)
    assert n == 1 and len(sent) == 1
    assert list(fired) == ["proximity:1h:100.5"]


def test_failed_send_is_not_marked_fired_and_is_retried(sandbox, monkeypatch, near_state):
    """Before: a Telegram outage marked the alert as sent and it was lost."""
    sent = _stub_deliver(monkeypatch, "failed")
    logs = []
    n, fired = alerts.run_once(state=near_state, fired={}, log=logs.append,
                               pair=near_state.pair)
    assert n == 0 and fired == {} and len(sent) == 1
    assert any("NOT delivered" in m for m in logs)

    # Next scan, Telegram is back: the same alert goes out.
    sent2 = _stub_deliver(monkeypatch, "sent")
    n2, fired2 = alerts.run_once(state=FakeState(100.0, [_lvl(100.5)]), fired=fired,
                                 log=lambda m: None, pair=near_state.pair)
    assert n2 == 1 and len(sent2) == 1 and "proximity:1h:100.5" in fired2


def test_printed_alert_counts_as_delivered(sandbox, monkeypatch, near_state):
    """Dry-run / no-credentials mode prints instead of sending; without marking
    it fired the same alert would be re-printed on every scan."""
    _stub_deliver(monkeypatch, "printed")
    n, fired = alerts.run_once(state=near_state, fired={}, log=lambda m: None,
                               pair=near_state.pair)
    assert n == 1 and "proximity:1h:100.5" in fired


def test_dry_run_does_not_persist_state(sandbox, monkeypatch, near_state):
    from detectors import paths
    _stub_deliver(monkeypatch, "printed")
    alerts.run_once(dry_run=True, state=near_state, fired={}, log=lambda m: None,
                    pair=near_state.pair)
    assert not paths.alerts_state(near_state.pair).exists()


def test_live_run_persists_state_atomically(sandbox, monkeypatch, near_state):
    from detectors import paths
    _stub_deliver(monkeypatch, "sent")
    alerts.run_once(state=near_state, fired={}, log=lambda m: None, pair=near_state.pair)
    assert "proximity:1h:100.5" in alerts._load_state(near_state.pair)
    leftovers = [p.name for p in paths.results_dir(near_state.pair).iterdir()
                 if ".tmp" in p.name]
    assert leftovers == []


def test_dedup_window():
    a = {"dedup_key": "k", "ts": "2026-08-28T15:00:00+00:00"}
    assert alerts._should_fire(a, {})
    assert not alerts._should_fire(a, {"k": "2026-08-28T12:00:00+00:00"})   # 3h ago
    assert alerts._should_fire(a, {"k": "2026-08-28T10:59:00+00:00"})       # >4h ago


def test_no_alert_for_a_neutralized_or_far_level():
    st = FakeState(100.0, [_lvl(100.5, cls="neutralized"), _lvl(110.0, lid="R002_1h_up_20260802_HI")])
    assert alerts.scan(st, alerts.ALERT_RECENCY_HOURS) == []


def test_alert_text_is_readable_at_shib_scale():
    price = 100.0 * SHIB_SCALE                    # ~5.96e-06
    level = 100.5 * SHIB_SCALE
    st = FakeState(price, [_lvl(level)], pair="SHIB_USDT")
    cands = alerts.scan(st, alerts.ALERT_RECENCY_HOURS)
    assert len(cands) == 1
    text = alerts.format_alert(cands[0], st.live_price, st)
    assert "$0.00 " not in text and "$0.00)" not in text      # the old ':.2f' output
    assert "$0.000005960" in text and "$0.000005990" in text  # price and level differ
    assert "+0.50%" in text


def test_dedup_keys_distinguish_cheap_levels():
    a = 100.5 * SHIB_SCALE
    b = 101.0 * SHIB_SCALE
    st = FakeState(100.0 * SHIB_SCALE, [_lvl(a), _lvl(b, lid="R002_1h_up_20260802_HI")])
    keys = {c["dedup_key"] for c in alerts.scan(st, alerts.ALERT_RECENCY_HOURS)}
    assert len(keys) == 2             # round(price, 6) made these one key


def test_event_alert_rules(sandbox):
    recent = (NOW - pd.Timedelta(hours=1)).isoformat()
    stale = (NOW - pd.Timedelta(hours=30)).isoformat()
    lid = "R001_1h_up_20260801_HI"

    def ev(etype, ts):
        return {"ts": ts, "type": etype, "level_id": lid, "level_price": 110.0,
                "source_range_id": lid[:-3]}

    lvl = _lvl(110.0, cls="strong", lid=lid)          # 10% away: no proximity
    got = {a["etype"] for a in alerts.scan(FakeState(100.0, [lvl],
           [ev("broken", recent), ev("reclaimed", recent)]), 4)}
    assert got == {"broken", "reclaimed"}             # strong: fire regardless of distance
    # touched needs the level within 2%; a stale event never fires
    assert alerts.scan(FakeState(100.0, [lvl], [ev("historical_level_touched", recent)]), 4) == []
    assert alerts.scan(FakeState(100.0, [lvl], [ev("broken", stale)]), 4) == []


def test_telegram_send_wrapper_keeps_its_bool_contract(monkeypatch):
    monkeypatch.delenv("TTRRONEV_TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TTRRONEV_TG_CHAT_ID", raising=False)
    quiet = lambda m: None
    assert alerts._deliver("x", dry_run=True, log=quiet) == "printed"
    assert alerts._deliver("x", dry_run=False, log=quiet) == "printed"     # no creds: log-only
    assert alerts._telegram_send("x", dry_run=True, log=quiet) is True
    assert alerts._telegram_send("x", dry_run=False, log=quiet) is False   # as before
