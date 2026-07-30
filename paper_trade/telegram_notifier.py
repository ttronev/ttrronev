"""Telegram notification sink — token-bucketed and sanitized.

Security posture (per operator instructions):
  * Bot token loaded from env. NEVER logged or printed; if you need to
    confirm the bot is wired up, send a 'bot online' message to the
    chat itself.
  * Rate-limited via a token bucket. Telegram caps ~30/s and ~20/min
    per chat; we cap at 15/min. Drops are logged to a local file,
    never echoed back to Telegram.
  * No sensitive content in messages: no API keys, no bot token, no
    full account balances. Trade events report DELTA equity (in $ and
    R), not balance specifics.

Async-only API: all sends return coroutines. Caller awaits them or
schedules via asyncio.create_task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

from paper_trade.config import (
    TG_BOT_TOKEN, TG_CHAT_ID, LOG_DIR,
)
from paper_trade.sqlite_store import log_runtime


# --- Sensitive pattern guard ------------------------------------------

# Heuristics. We strip rather than refuse — caller mistakes shouldn't
# silently drop alerts. If a pattern is a false positive, it shows up
# as a [REDACTED] tag in Telegram and we know to fix the call site.
_SENSITIVE_PATTERNS = [
    # Telegram bot token format: <numeric>:<35char-base64ish>
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    # Bybit / Binance API key (32-64 hex/alphanum). Conservative to avoid
    # nuking legitimate hashes; require key/secret keyword nearby.
    re.compile(r"(?:api[_-]?key|api[_-]?secret|secret)\s*[:=]\s*\S{16,}",
               re.IGNORECASE),
    # Bearer tokens.
    re.compile(r"Bearer\s+\S{16,}", re.IGNORECASE),
]


def _sanitize(text: str) -> str:
    out = text
    for pat in _SENSITIVE_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


# --- Token bucket -----------------------------------------------------

@dataclass
class _TokenBucket:
    rate_per_sec: float
    burst: int
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = float(self.burst)

    def try_take(self, n: int = 1) -> bool:
        now = time.monotonic()
        delta = now - self.last_refill
        self.last_refill = now
        self.tokens = min(self.burst, self.tokens + delta * self.rate_per_sec)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


# --- Notifier ---------------------------------------------------------

_LOG = logging.getLogger("paper_trade.telegram")


class TelegramNotifier:
    """Async Telegram client. Configuration read from env at construct
    time; if missing, the notifier becomes a no-op (so dev runs without
    Telegram still work cleanly)."""

    def __init__(self, *, rate_per_min: int = 15, burst: int = 5) -> None:
        # Read env values into local refs only — never store on self by
        # name to make accidental logging harder.
        self._token: Optional[str] = TG_BOT_TOKEN
        self._chat_id: Optional[str] = TG_CHAT_ID
        self.enabled: bool = bool(self._token and self._chat_id)
        self._bucket = _TokenBucket(
            rate_per_sec=rate_per_min / 60.0,
            burst=burst,
        )
        self._drops_log: Path = LOG_DIR / "telegram_drops.jsonl"
        self._session: Optional[aiohttp.ClientSession] = None
        self._n_drops_total = 0

    async def __aenter__(self) -> "TelegramNotifier":
        if self.enabled:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # --- Internal send ------------------------------------------------

    async def _send_raw(self, text: str) -> bool:
        """Returns True if Telegram accepted the message (or if disabled)."""
        if not self.enabled or self._session is None:
            return True
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with self._session.post(url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    return True
                # Don't echo response body to logs — it can contain the
                # token in error responses on some Telegram error paths.
                await resp.read()
                _LOG.warning("telegram send returned status=%s", resp.status)
                return False
        except Exception as e:
            # NB: never log e.args verbatim — could include the URL with
            # the token. Log only the exception class.
            _LOG.warning("telegram send failed: %s", type(e).__name__)
            return False

    def _record_drop(self, text: str, reason: str) -> None:
        self._n_drops_total += 1
        try:
            with self._drops_log.open("a", encoding="utf-8") as f:
                rec = {
                    "ts_ms": int(time.time() * 1000),
                    "reason": reason,
                    "n_drops_total": self._n_drops_total,
                    # Truncate text and keep first 200 chars only.
                    "text_preview": text[:200],
                }
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        log_runtime("telegram_drop", {"reason": reason, "n": self._n_drops_total})

    # --- Public API ---------------------------------------------------

    async def send(self, text: str) -> bool:
        """Sanitize, rate-limit, send. Returns True if sent."""
        text = _sanitize(text)
        if not self._bucket.try_take(1):
            self._record_drop(text, "rate_limited")
            return False
        ok = await self._send_raw(text)
        if not ok:
            self._record_drop(text, "send_failed")
        return ok

    # --- Pre-built event templates -----------------------------------

    @staticmethod
    def _fmt_price(p: Optional[float]) -> str:
        """Render a price for display. Conventions:
          >= 100   -> 2 decimals with thousands separator
          >= 1     -> 2 decimals  (matches spec example: '84.00')
          >= 0.01  -> 4 decimals  (sub-dollar pairs)
          else     -> 6 decimals  (sub-cent / micro caps)
        """
        if p is None:
            return "?"
        if abs(p) >= 100:
            return f"{p:,.2f}"
        if abs(p) >= 1:
            return f"{p:.2f}"
        if abs(p) >= 0.01:
            return f"{p:.4f}"
        return f"{p:.6f}"

    @staticmethod
    def _fmt_hm(ts) -> str:
        """HH:MM 24h UTC."""
        if ts is None:
            return "??:??"
        if hasattr(ts, "strftime"):
            return ts.strftime("%H:%M")
        return str(ts)

    @staticmethod
    def _fmt_setup_id(pair: str, setup_num: int) -> str:
        """SOL-15 style. Pair shortens to its base symbol."""
        base = pair.split("/")[0] if "/" in pair else pair
        return f"{base}-{setup_num}"

    @staticmethod
    def _fib_ladder_block(fibs: dict[str, float]) -> str:
        """Monospace fib ladder. fibs is {label: price} for keys
        ('1.2','1.0','0.75','0.5','0.3','0'). HTML <pre> preserves
        spacing in Telegram. Uses 2-decimal display to match spec."""
        # Ladder order from spec: 1.2 / 1.0 / 0.75 / 0.5 / 0.3 / 0
        order = [("1.2 ", "1.2"), ("1.0 ", "1.0"), ("0.75", "0.75"),
                 ("0.5 ", "0.5"), ("0.3 ", "0.3"), ("0   ", "0")]
        rendered = [(label, TelegramNotifier._fmt_price(fibs.get(key)))
                    for label, key in order]
        # Right-align all price strings on the longest one.
        width = max(len(p) for _, p in rendered)
        body = "\n".join(f"  {label} {p:>{width}}" for label, p in rendered)
        return f"<pre>{body}</pre>"

    # -- Static-format messages (unchanged from prior) ---------------

    async def bot_online(self, phase: str, pairs: tuple[str, ...],
                         started_iso: Optional[str] = None,
                         reset_archive: Optional[str] = None) -> None:
        """Bot-online banner.

        Args:
            phase: phase tag (e.g. '1a').
            pairs: pair tuple.
            started_iso: current UTC datetime, second-precision string
                (e.g. '2026-05-10 19:23:47 UTC'). Operator uses this to
                disambiguate which run is which when reading the
                Telegram log later.
            reset_archive: when non-None, the relative path to the
                archived prior SQLite (e.g.
                'paper_trade/data/archive/paper_trade_pre_20260510_checkpoint_fix.sqlite').
                Triggers the FRESH-START banner explaining setup
                numbering restarts at 1.
        """
        lines = [
            f"<b>Bot online</b> [{phase}]",
            f"Pairs: {', '.join(pairs)}",
            f"v1.4 — secondary-only, regime gate 30%",
        ]
        if started_iso:
            lines.append(f"Started: {started_iso}")
        if reset_archive:
            lines.append("")
            lines.append(
                f"<b>Fresh start</b> — previous database archived to:\n"
                f"  <code>{reset_archive}</code>\n"
                f"Setup numbering starts at 1."
            )
        await self.send("\n".join(lines))

    async def startup_summary(self, per_pair: list[dict]) -> None:
        """Per-pair confirmation that the engine has been brought up to
        the latest buffered bar. Each row is a dict with keys:
          pair, status (str: 'restored'/'cold_bootstrap'),
          checkpoint_ts (str or '-'),
          buffer_ts (str), bars_replayed (int),
          last_5m_after (str).
        Operator uses this to verify nothing is stale before live runs.

        The `last_5m_after` field MUST be the timestamp of the latest
        5m bar the engine has now processed (i.e., the engine's
        timeline cursor after restore + catch-up replay completes).
        Lets the operator visually confirm "engine is current to-the-
        minute" against wall-clock at startup.
        """
        lines: list[str] = []
        for row in per_pair:
            pair = row.get("pair", "?")
            status = row.get("status", "?")
            cp = row.get("checkpoint_ts", "-")
            buf_ts = row.get("buffer_ts", "?")
            replayed = int(row.get("bars_replayed", 0))
            after_ts = row.get("last_5m_after", "?")
            candidate = row.get("candidate", "no active setup")
            lines.append(
                f"  <b>{pair}</b> [{status}]\n"
                f"    checkpoint:    {cp}\n"
                f"    buffer top:    {buf_ts}\n"
                f"    replayed:      {replayed} bars\n"
                f"    engine 5m now: {after_ts}\n"
                f"    candidate:     {candidate}"
            )
        body = "\n".join(lines) if lines else "  (no pairs)"
        await self.send(
            f"<b>STARTUP — engine sync</b>\n{body}"
        )

    async def bot_offline(self, reason: str) -> None:
        await self.send(f"<b>Bot offline</b>: {reason}")

    async def heartbeat(self, uptime_min: int, pair_status: dict[str, str],
                        n_open_positions: int) -> None:
        rows = "\n".join(f"  {p}: {s}" for p, s in pair_status.items())
        await self.send(
            f"<b>Heartbeat</b> ({uptime_min}m up)\n"
            f"Open positions: {n_open_positions}\n{rows}"
        )

    async def error(self, kind: str, detail: str) -> None:
        det = detail[:300]
        await self.send(f"<b>ERROR</b> [{kind}]\n{det}")

    async def validation_alert(self, day: str, pair: str,
                               streaming_n: int, batch_n: int) -> None:
        await self.send(
            f"<b>VALIDATION DIVERGENCE</b> {day} {pair}\n"
            f"streaming events: {streaming_n}\n"
            f"batch events:     {batch_n}\n"
            f"INVESTIGATE — see SQLite validation table"
        )

    # -- Trade lifecycle messages (Phase 1a redesign) ---------------

    async def setup_armed(self, pair: str, setup_num: int, direction: str,
                          swing_high: float, swing_low: float,
                          swing_high_ts, swing_low_ts,
                          swing_size_pct: float,
                          fibs: dict[str, float],
                          primary_limit: float) -> None:
        sid = self._fmt_setup_id(pair, setup_num)
        await self.send(
            f"🟡 <b>{sid}</b> SETUP ARMED — {direction}\n"
            f"Pair: {pair}\n"
            f"Swing: {self._fmt_price(swing_high)} @ {self._fmt_hm(swing_high_ts)} "
            f"→ {self._fmt_price(swing_low)} @ {self._fmt_hm(swing_low_ts)} "
            f"({swing_size_pct:.2f}%)\n"
            f"Fibs:\n{self._fib_ladder_block(fibs)}\n"
            f"Primary limit @ {self._fmt_price(primary_limit)} placed"
        )

    async def primary_triggered(self, pair: str, setup_num: int,
                                fib_1_0: float, entry: float,
                                sl: float, tp: float) -> None:
        sid = self._fmt_setup_id(pair, setup_num)
        await self.send(
            f"🟠 <b>{sid}</b> PRIMARY TRIGGERED\n"
            f"Pair: {pair}\n"
            f"Wicked fib_1.0 @ {self._fmt_price(fib_1_0)}\n"
            f"Secondary limit @ {self._fmt_price(entry)} (entry)\n"
            f"SL: {self._fmt_price(sl)} (close-based fib_0.3)\n"
            f"TP: {self._fmt_price(tp)} (fib_1.0)"
        )

    async def filled(self, pair: str, setup_num: int,
                     entry: float, sl: float, tp: float,
                     size_coins: float, leverage: float,
                     intended_risk: float, realized_risk: float,
                     base_symbol: Optional[str] = None) -> None:
        sid = self._fmt_setup_id(pair, setup_num)
        if base_symbol is None:
            base_symbol = pair.split("/")[0] if "/" in pair else pair
        risk_str = f"${realized_risk:.2f}"
        if abs(realized_risk - intended_risk) > 0.01:
            risk_str += f" (intended ${intended_risk:.2f}; lev cap)"
        await self.send(
            f"🟢 <b>{sid}</b> FILLED\n"
            f"Pair: {pair}\n"
            f"Entry: {self._fmt_price(entry)} (fib_0.5)\n"
            f"SL: {self._fmt_price(sl)} | TP: {self._fmt_price(tp)}\n"
            f"Position size: {size_coins:.4f} {base_symbol} @ {leverage:.1f}x leverage\n"
            f"Risk: {risk_str}"
        )

    @staticmethod
    def _fmt_signed_dollars(v: float) -> str:
        """Render '+$1.50' / '-$1.00' / '+$0.00' (sign before dollar sign)."""
        sign = "+" if v >= 0 else "-"
        return f"{sign}${abs(v):.2f}"

    async def closed_tp(self, pair: str, setup_num: int,
                        exit_price: float, r_planned: float, r_realized: float,
                        pnl: float, equity: float) -> None:
        """`r_planned` — the planned reward at TP relative to risk
        (= r_planned from the engine; varies per swing). Annotated on
        the Exit line so the user sees the intended payout vs the
        realized R below it."""
        sid = self._fmt_setup_id(pair, setup_num)
        sign_r = "+" if r_realized >= 0 else ""
        sign_pl = "+" if r_planned >= 0 else ""
        await self.send(
            f"✅ <b>{sid}</b> TP HIT\n"
            f"Pair: {pair}\n"
            f"Exit: {self._fmt_price(exit_price)} ({sign_pl}{r_planned:.2f}R planned)\n"
            f"Realized R: {sign_r}{r_realized:.2f}\n"
            f"PnL: {self._fmt_signed_dollars(pnl)}\n"
            f"Equity: ${equity:.2f}"
        )

    async def closed_sl(self, pair: str, setup_num: int,
                        exit_price: float, r_realized: float,
                        pnl: float, equity: float) -> None:
        sid = self._fmt_setup_id(pair, setup_num)
        sign_r = "+" if r_realized >= 0 else ""
        await self.send(
            f"❌ <b>{sid}</b> SL HIT\n"
            f"Pair: {pair}\n"
            f"Exit: {self._fmt_price(exit_price)} (close-based)\n"
            f"Realized R: {sign_r}{r_realized:.2f}\n"
            f"PnL: {self._fmt_signed_dollars(pnl)}\n"
            f"Equity: ${equity:.2f}"
        )

    async def cancelled(self, pair: str, setup_num: int,
                        reason: str, detail: str) -> None:
        """`reason` ∈ {invalidation_pre_arm, invalidation_pre_fill,
        primary_sl_pre_secondary_fill}. `detail` is the human-readable
        explanation (e.g., '5m close past fib_0 at 82.95 @ 15:20 UTC')."""
        sid = self._fmt_setup_id(pair, setup_num)
        await self.send(
            f"⚪ <b>{sid}</b> CANCELLED\n"
            f"Pair: {pair}\n"
            f"Reason: {reason}\n"
            f"Detail: {detail}"
        )

    # -- Daily summary --------------------------------------------------

    async def daily_summary(self, day_str: str,
                            n_closed: int, n_tp: int, n_sl: int,
                            r_sum: float, pnl: float,
                            equity_now: float, equity_start: float,
                            per_pair: list[dict],
                            n_armed: int, n_filled: int, n_cancelled: int) -> None:
        """`per_pair`: list of dicts with keys pair, w, l, r_sum."""
        sign_r = "+" if r_sum >= 0 else ""
        if equity_start > 0:
            pct = (equity_now - equity_start) / equity_start * 100
            sign_pct = "+" if pct >= 0 else ""
            equity_line = f"Equity: ${equity_now:.2f} ({sign_pct}{pct:.2f}% since start)"
        else:
            equity_line = f"Equity: ${equity_now:.2f}"

        per_pair_lines = []
        for row in per_pair:
            w = row.get("w", 0)
            l = row.get("l", 0)
            r = row.get("r_sum", 0.0)
            sign_pr = "+" if r >= 0 else ""
            if w + l == 0:
                tag = "(idle)"
            else:
                tag = f"({sign_pr}{r:.2f}R)"
            per_pair_lines.append(f"  {row['pair']}: {w}W {l}L {tag}")

        per_pair_block = "\n".join(per_pair_lines) if per_pair_lines else "  (none)"

        await self.send(
            f"📊 <b>DAILY SUMMARY</b> — {day_str}\n"
            f"Trades closed: {n_closed} ({n_tp} TP / {n_sl} SL)\n"
            f"R sum: {sign_r}{r_sum:.2f}\n"
            f"PnL: {self._fmt_signed_dollars(pnl)}\n"
            f"{equity_line}\n"
            f"\n"
            f"By pair:\n{per_pair_block}\n"
            f"\n"
            f"Setups armed today: {n_armed}\n"
            f"Setups filled today: {n_filled}\n"
            f"Setups cancelled today: {n_cancelled}"
        )


__all__ = ["TelegramNotifier"]
