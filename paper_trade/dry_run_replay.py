"""Dry-run integration test: feed historical bars at accelerated speed
through the FULL Phase 1a pipeline (engine + SQLite + Telegram + health
endpoint).

Goal: catch integration bugs that the streaming-parity test (logic-
only, no IO) doesn't exercise — Telegram rate-limiting under bursts,
SQLite contention under concurrent writes, health endpoint behavior,
event ordering across asyncio boundaries.

Usage:
    python -m paper_trade.dry_run_replay \\
        --pairs SOL/USDT AVAX/USDT LINK/USDT \\
        --start 2026-04-01 --end 2026-05-04 \\
        --speed-days-per-min 1.0

`--speed-days-per-min` controls the replay rate. 1 day/min = 5min-bar
frames every 200ms. The default keeps a ~3hr replay for 1 month of
data, which is enough to surface integration issues without being
boring.

NB: This run writes to a SEPARATE SQLite file (`paper_trade_dryrun.sqlite`)
so it doesn't pollute the live store. Telegram will fire if creds are
set in env — set TTRRONEV_TG_DRYRUN_SUPPRESS=1 to bypass.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from datetime import timezone
from pathlib import Path

import aiohttp
import pandas as pd

# Re-route SQLite to a dryrun file BEFORE any sqlite_store import.
os.environ.setdefault("TTRRONEV_PHASE", "1a-dryrun")

from paper_trade import config as _cfg  # noqa: E402

_dry_path = _cfg.DATA_DIR / "paper_trade_dryrun.sqlite"
_cfg.SQLITE_PATH = _dry_path

# The store reads SQLITE_PATH at first connect, so re-importing it now
# picks up the override.
from paper_trade.bar_buffer import Bar, PairBuffers   # noqa: E402
from paper_trade.config import EXEC_TF, TREND_TF, PAIRS, PHASE   # noqa: E402
from paper_trade.event_sink import EventSink            # noqa: E402
from paper_trade.health_server import HealthState, start_server  # noqa: E402
from paper_trade.risk_manager import RiskManager        # noqa: E402
from paper_trade.sqlite_store import init_schema, log_runtime  # noqa: E402
from paper_trade.streaming_engine import StreamingEngine  # noqa: E402
from paper_trade.telegram_notifier import TelegramNotifier  # noqa: E402


_LOG = logging.getLogger("paper_trade.dryrun")


def _load_pair_csv(pair: str, tf: str, start: pd.Timestamp,
                   end: pd.Timestamp) -> pd.DataFrame:
    pf = pair.replace("/", "_")
    df = pd.read_csv(_cfg.REPO_ROOT / "data" / "raw" / f"{pf}_{tf}.csv")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df[(df["timestamp"] >= start) & (df["timestamp"] <= end)].reset_index(drop=True)


async def _replay_pair(pair: str,
                       df_5m_window: pd.DataFrame,
                       df_1h_full: pd.DataFrame,
                       buffers: PairBuffers,
                       streaming: StreamingEngine,
                       loop: asyncio.AbstractEventLoop,
                       speed_days_per_min: float,
                       health: HealthState) -> None:
    """Pre-load 1h full history, then drip 5m bars at accelerated speed."""
    # Pre-load 1h up to the start (anything earlier the engine needs).
    start_ts = df_5m_window["timestamp"].iloc[0]
    pre_1h = df_1h_full[df_1h_full["timestamp"] < start_ts]
    for _, row in pre_1h.iterrows():
        buffers.b1h.append_closed(
            Bar(int(pd.Timestamp(row["timestamp"]).value // 10**6),
                float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]), float(row["volume"])),
            persist=True,
        )
    _LOG.info("%s: pre-loaded %d 1h bars", pair, len(pre_1h))

    # 5m bar interval in real seconds at requested speed.
    # speed_days_per_min: 1.0 => 24h compressed to 1min => 5m bar = 0.2083ms
    sec_per_5m = (5.0 * 60.0) / (speed_days_per_min * 1440.0)

    # Iterate 5m bars in window. Whenever a new 1h bar's timestamp
    # falls between two 5m bars, also append it.
    df_1h_window = df_1h_full[df_1h_full["timestamp"] >= start_ts].reset_index(drop=True)
    j_1h = 0
    n5 = len(df_5m_window)
    last_pct = -1
    for i in range(n5):
        row = df_5m_window.iloc[i]
        t5 = pd.Timestamp(row["timestamp"])
        # Append any 1h bars whose timestamp is <= t5.
        while j_1h < len(df_1h_window) and df_1h_window.iloc[j_1h]["timestamp"] <= t5:
            r1 = df_1h_window.iloc[j_1h]
            buffers.b1h.append_closed(
                Bar(int(pd.Timestamp(r1["timestamp"]).value // 10**6),
                    float(r1["open"]), float(r1["high"]), float(r1["low"]),
                    float(r1["close"]), float(r1["volume"])),
                persist=True,
            )
            j_1h += 1
        bar = Bar(
            timestamp_ms=int(t5.value // 10**6),
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]),
        )
        buffers.b5m.append_closed(bar, persist=True)
        health.mark_bar(pair, bar.timestamp_ms)
        # Engine call in executor so we exercise the same code path as live.
        await loop.run_in_executor(None, streaming.on_new_bar_5m)
        # Pacing.
        await asyncio.sleep(sec_per_5m)
        pct = int(i * 100 / max(1, n5 - 1))
        if pct != last_pct and pct % 5 == 0:
            _LOG.info("%s: %d%% (%d/%d)", pair, pct, i, n5)
            last_pct = pct


async def amain(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    init_schema()
    log_runtime("start", {"phase": PHASE, "mode": "dryrun"})

    pairs = tuple(args.pairs)
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")

    pair_buffers: dict[str, PairBuffers] = {p: PairBuffers(p) for p in pairs}
    df_5m: dict[str, pd.DataFrame] = {}
    df_1h: dict[str, pd.DataFrame] = {}
    for pair in pairs:
        # 5m sliced; 1h full to give regime gate its lookback.
        df_5m[pair] = _load_pair_csv(pair, EXEC_TF, start, end)
        df_1h_all = pd.read_csv(
            _cfg.REPO_ROOT / "data" / "raw" / f"{pair.replace('/', '_')}_{TREND_TF}.csv"
        )
        df_1h_all["timestamp"] = pd.to_datetime(df_1h_all["timestamp"], unit="ms", utc=True)
        df_1h[pair] = df_1h_all[df_1h_all["timestamp"] <= end].reset_index(drop=True)
        _LOG.info("%s: 5m=%d  1h=%d", pair, len(df_5m[pair]), len(df_1h[pair]))

    risk = RiskManager()
    health = HealthState(equity=risk.equity)
    loop = asyncio.get_running_loop()

    async with TelegramNotifier() as notifier:
        runner, site = await start_server(health)
        try:
            from paper_trade.setup_numbering import SetupNumberAllocator
            setup_num_alloc = SetupNumberAllocator(pairs)
            sink = EventSink(risk=risk, health=health, notifier=notifier,
                             buffers_by_pair=pair_buffers,
                             setup_num_alloc=setup_num_alloc,
                             loop=loop)
            streaming = {p: StreamingEngine(pair=p, buffers=pair_buffers[p], on_event=sink)
                         for p in pairs}
            if not os.environ.get("TTRRONEV_TG_DRYRUN_SUPPRESS"):
                await notifier.bot_online("1a-dryrun", pairs)

            tasks = []
            for p in pairs:
                tasks.append(asyncio.create_task(
                    _replay_pair(p, df_5m[p], df_1h[p],
                                 pair_buffers[p], streaming[p], loop,
                                 args.speed_days_per_min, health),
                    name=f"replay:{p}",
                ))
            await asyncio.gather(*tasks)
            _LOG.info("dry-run replay complete; final equity=$%.2f", risk.equity)
        finally:
            try:
                if not os.environ.get("TTRRONEV_TG_DRYRUN_SUPPRESS"):
                    await notifier.bot_offline("dryrun complete")
            except Exception:
                pass
            await site.stop()
            await runner.cleanup()
            log_runtime("stop", {"mode": "dryrun"})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pairs", nargs="+", default=list(PAIRS))
    p.add_argument("--start", default="2026-04-15")
    p.add_argument("--end", default="2026-05-04")
    p.add_argument("--speed-days-per-min", type=float, default=2.0,
                   help="Replay speed: how many calendar days fit in 1 wall-clock minute.")
    args = p.parse_args()
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
