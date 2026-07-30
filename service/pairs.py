"""
service/pairs.py — the single pair roster + per-TF service config.

Adding a pair = ONE line in PAIRS + the raw CSVs in data/raw/
({PAIR}_{tf}.csv for every TF in WORKER_TFS; bootstrap 5m via
data/bootstrap_5m.py, others via data/okx_fetch.py) + worker restart.
No path edits anywhere — everything resolves through detectors/paths.py.
"""
from __future__ import annotations

# Pair format is the full CSV prefix (see detectors/paths.py).
PAIRS = ["SOL_USDT"]

# TFs the worker keeps regenerated, slowest first (chain order habit).
WORKER_TFS = ["1w", "1d", "4h", "2h", "1h", "5m"]

# TFs that have range memory / strength (the L5 chain). 5m is layer-1 only.
MEMORY_TFS = ["1w", "1d", "4h", "2h", "1h"]

# Bar interval per TF, milliseconds. All boundaries align to the epoch
# (00:00 UTC); 1w is scheduled off the daily boundary instead (see
# scheduler.py) because epoch-week starts on a Thursday while OKX 1Wutc
# candles do not.
TF_MS = {
    "5m": 5 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "1w": 7 * 24 * 60 * 60_000,
}

# Regeneration window per TF, in days of candles fed to the detector.
# None = full history. Bounded windows keep the per-bar regen cheap
# (5m on full history is tens of MB and minutes of compute every 5
# minutes — see the service spec). Ranges near the window's left edge
# are untrusted by construction; windows are sized so anything near
# current price is far inside.
REGEN_WINDOW_DAYS = {
    "5m": 45,
    "1h": 365,
    "2h": 365,
    "4h": 730,
    "1d": None,
    "1w": None,
}

# Seconds to wait after a bar boundary before fetching (exchange needs a
# moment to publish the closed candle).
CLOSE_BUFFER_S = 15

# One retry if the closed bar isn't published yet on first fetch.
FETCH_RETRY_DELAY_S = 20

# Live price poll period (display + proximity only).
LIVE_PRICE_INTERVAL_S = 15

# The L5 chain (cleanness -> nesting -> known_at -> range memory ->
# strength) runs on the worker's hourly structural cycle, after ALL due
# detectors — at most hourly by construction, never between two detector
# regens of the same boundary. (No interval constant: the batching IS the
# guarantee.)

# Same worker error is re-alerted to Telegram at most once per this.
ERROR_ALERT_COOLDOWN_S = 3600
