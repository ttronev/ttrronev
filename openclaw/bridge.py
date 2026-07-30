"""OpenClaw bridge for ttrronev.

OpenClaw is the AI agent platform that ttrronev will plug into for higher-level
reasoning, memory recall, and supervision. This module is the thin adapter that
translates between the bot's internal events and OpenClaw's API.

Until OpenClaw integration is wired up, all calls here are no-ops that log
locally so the bot can run end-to-end without a network dependency.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OpenClawConfig:
    base_url: str | None = None
    api_key: str | None = None
    agent_id: str | None = None
    enabled: bool = False

    @classmethod
    def from_env(cls) -> "OpenClawConfig":
        return cls(
            base_url=os.getenv("OPENCLAW_BASE_URL"),
            api_key=os.getenv("OPENCLAW_API_KEY"),
            agent_id=os.getenv("OPENCLAW_AGENT_ID"),
            enabled=os.getenv("OPENCLAW_ENABLED", "false").lower() == "true",
        )


class OpenClawBridge:
    """Adapter between ttrronev events and the OpenClaw agent platform.

    Event flow:

        ttrronev   ->   OpenClawBridge.emit_*   ->   OpenClaw
        OpenClaw   ->   hooks/<event>.py        ->   ttrronev (advice / overrides)

    When `config.enabled` is False, every emit_* method writes the event to
    `logs/trades/openclaw_offline.jsonl` and returns immediately. This keeps the
    bot fully functional in dev / paper mode without an OpenClaw account.
    """

    OFFLINE_LOG = Path("logs/trades/openclaw_offline.jsonl")

    def __init__(self, config: OpenClawConfig | None = None):
        self.config = config or OpenClawConfig.from_env()
        if self.config.enabled and not (self.config.base_url and self.config.api_key):
            raise ValueError(
                "OpenClaw is enabled but OPENCLAW_BASE_URL or OPENCLAW_API_KEY is missing."
            )
        if not self.config.enabled:
            self.OFFLINE_LOG.parent.mkdir(parents=True, exist_ok=True)

    # --- outbound events --------------------------------------------------------------------

    def emit_signal(self, payload: dict[str, Any]) -> None:
        """Strategy generated a candidate signal — let OpenClaw see/critique it."""
        self._send("signal", payload)

    def emit_fill(self, payload: dict[str, Any]) -> None:
        """An order filled (paper or live)."""
        self._send("fill", payload)

    def emit_risk_event(self, payload: dict[str, Any]) -> None:
        """Drawdown breach, daily loss limit, kill-switch trigger, etc."""
        self._send("risk", payload)

    def emit_heartbeat(self, payload: dict[str, Any]) -> None:
        """Periodic 'I'm alive + here are my positions' beacon."""
        self._send("heartbeat", payload)

    # --- inbound: synced via openclaw/hooks/*.py and openclaw/memory_sync.md ---------------

    def pull_directives(self) -> list[dict[str, Any]]:
        """Fetch any pending advice / overrides issued by the OpenClaw agent."""
        if not self.config.enabled:
            return []
        # TODO: GET {base_url}/agents/{agent_id}/directives
        return []

    # --- internals --------------------------------------------------------------------------

    def _send(self, event_type: str, payload: dict[str, Any]) -> None:
        envelope = {"event": event_type, "payload": payload}
        if not self.config.enabled:
            with self.OFFLINE_LOG.open("a") as f:
                f.write(json.dumps(envelope) + "\n")
            return
        # TODO: POST {base_url}/agents/{agent_id}/events with bearer auth.
        logger.debug("openclaw send stub: %s", envelope)
