# archive/ — frozen trees

paper_trade/ — Phase 1a shadow tracker (May 2026), Binance stream, SQLite store, parity drills; its ideas return as the ledger (A1), the journal (B4) and paper execution (B7).

ml_models/ — online learner and self-evolving optimizer scaffolds; superseded by the scorecard and gate (A2, A3).

openclaw/ — bridge to an external agent runtime; superseded by the in-app agent (B8).

strategies/ — the first strategy notes and log; superseded by the strategy spec (B11).

exchange_stubs/ — Bybit client and paper-trading stubs; superseded by Hyperliquid (B1, B7).

requirements.txt — the archived stack's dependencies.

paper_trade/deploy/ — the paper-trade era host watchdog and systemd unit (health_check.sh, health_check.cron, paper_trade.service).

Frozen: not importable or runnable as-is; imports name the pre-R0 layout (for example `archive/paper_trade/level_proximity.py` imports `backtesting.level_features`). See the git history before the R0 commit to run any of it. Excluded from pytest collection, ruff, mypy and the Docker image; nothing in production imports from here (`scripts/check_layout.py` checks).
