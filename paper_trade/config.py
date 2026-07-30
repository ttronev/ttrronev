"""Phase 1 paper-trade config — single source of truth.

Anything that needs to be tweaked between dev / staging / prod lives
here, not scattered across modules.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Pairs / timeframes -----------------------------------------------

PAIRS: tuple[str, ...] = ("SOL/USDT", "AVAX/USDT", "LINK/USDT")
EXEC_TF: str = "5m"
TREND_TF: str = "1h"
INTRABAR_TF: str = "1m"   # sub-5m fill detection (live-only, no backtest equivalent)

# --- Buffer sizes -----------------------------------------------------
# CRITICAL: the engine re-checks the regime gate for EVERY BOS event
# in the 5m buffer on each run, not just the latest bar. So the 1h
# buffer must contain enough bars that EVERY 5m bar maps to a 1h
# index >= regime_lookback_h (=2160). Otherwise returns_pct[j] = 0 in
# the warm-up zone and the gate falsely fails for older buffered bars.
#
# 5m buffer of N bars = N/12 hours. So:
#     BUFFER_1H  >=  regime_lookback_h + (BUFFER_5M / 12)
#                 =  2160 + 583  =  ~2743 (for BUFFER_5M=7000)
# We size to 3500 for safety. (BUFFER_1H is small in memory; <1MB.)
BUFFER_5M: int = 7000
BUFFER_1H: int = 3500

# Minimum bars required before the engine is allowed to emit signals.
# Mirrors v1.4 backtest preconditions.
MIN_5M_FOR_ENGINE: int = 200    # init_bars=50 + handful of bars to detect first ratchet
# 1h minimum: regime lookback (2160) + the 5m buffer's span in 1h
# units, so the gate evaluates correctly for every bar in the buffer.
MIN_1H_FOR_REGIME: int = 90 * 24 + (BUFFER_5M // 12) + 24

# --- Risk model (Phase 1a simulated; Phase 1b applies to real $) ------

ACCOUNT_START: float = 100.0       # starting equity in quote (USDT)
RISK_DOLLARS: float = 1.0          # fixed $ risk per trade
MAX_CONCURRENT: int = 3            # one position per pair, max 3 across portfolio
AGGREGATE_RISK_CAP: float = 3.0    # max simultaneous $-risk deployed
MAX_LEVERAGE: float = 10.0         # Phase 1b: never above this
TAKER_FEE: float = 0.00055         # Bybit taker; matches backtest

# Daily / weekly halt thresholds (Phase 1b only — Phase 1a logs but doesn't halt).
DAILY_LOSS_HALT: float = -5.0
WEEKLY_LOSS_HALT: float = -15.0

# --- Paths ------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
DATA_DIR: Path = REPO_ROOT / "paper_trade" / "data"
SQLITE_PATH: Path = DATA_DIR / "paper_trade.sqlite"
LOG_DIR: Path = REPO_ROOT / "paper_trade" / "logs"
EQUITY_CSV: Path = DATA_DIR / "equity_curve.csv"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# --- External services ------------------------------------------------

BINANCE_WS_URL: str = "wss://stream.binance.com:9443/stream"
BINANCE_REST_URL: str = "https://api.binance.com/api/v3/klines"

# Telegram credentials read from env at runtime; missing creds = log-only
# mode (no notifications sent, no error raised — useful for local dev).
TG_BOT_TOKEN: str | None = os.environ.get("TTRRONEV_TG_BOT_TOKEN")
TG_CHAT_ID: str | None = os.environ.get("TTRRONEV_TG_CHAT_ID")

# Bybit (Phase 1b only).
BYBIT_API_KEY: str | None = os.environ.get("TTRRONEV_BYBIT_API_KEY")
BYBIT_API_SECRET: str | None = os.environ.get("TTRRONEV_BYBIT_API_SECRET")
BYBIT_ENV: str = os.environ.get("TTRRONEV_BYBIT_ENV", "testnet")  # testnet / mainnet

# --- Health server ----------------------------------------------------

HEALTH_HOST: str = "127.0.0.1"
HEALTH_PORT: int = 8765
HEARTBEAT_SECONDS: int = 3600       # Telegram heartbeat every 1h
                                    # (was 30 min; bumped per operator
                                    # request so chat doesn't get
                                    # noisy on quiet pairs while still
                                    # confirming the bot is alive +
                                    # surfacing any in-flight setups)
HEALTH_STALE_BARS: int = 3           # /health = stale if no closed 5m bar in last 3 intervals

# --- Phase tag --------------------------------------------------------

PHASE: str = os.environ.get("TTRRONEV_PHASE", "1a")  # "1a" or "1b"

# --- Strategy variant -------------------------------------------------
# Source of truth for engine config: re-uses backtest CONFIG so any change
# is in one place. Risk params overridden to live values below.

from strategies.secondary_only_v1 import CONFIG as _BACKTEST_CFG  # noqa: E402

ENGINE_CONFIG: dict = dict(_BACKTEST_CFG)
ENGINE_CONFIG["regime_gate_pct"] = 30.0
ENGINE_CONFIG["regime_duration_bars"] = 0
# Note: engine internally derives risk from cfg["risk_pct"]. We override
# at instantiation time in run.py so the same CONFIG can be re-used by
# the daily validator (which compares to the real backtest engine).

__all__ = [
    "PAIRS", "EXEC_TF", "TREND_TF", "INTRABAR_TF",
    "BUFFER_5M", "BUFFER_1H",
    "MIN_5M_FOR_ENGINE", "MIN_1H_FOR_REGIME",
    "ACCOUNT_START", "RISK_DOLLARS", "MAX_CONCURRENT",
    "AGGREGATE_RISK_CAP", "MAX_LEVERAGE", "TAKER_FEE",
    "DAILY_LOSS_HALT", "WEEKLY_LOSS_HALT",
    "REPO_ROOT", "DATA_DIR", "SQLITE_PATH", "LOG_DIR", "EQUITY_CSV",
    "BINANCE_WS_URL", "BINANCE_REST_URL",
    "TG_BOT_TOKEN", "TG_CHAT_ID",
    "BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_ENV",
    "HEALTH_HOST", "HEALTH_PORT", "HEARTBEAT_SECONDS", "HEALTH_STALE_BARS",
    "PHASE", "ENGINE_CONFIG",
]
