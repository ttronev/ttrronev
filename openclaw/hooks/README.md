# openclaw/hooks/

Event hooks invoked by `openclaw/bridge.py`. Each hook is a small Python module
that handles a single event type from OpenClaw. Hooks are loaded by name; the
bridge does `importlib.import_module(f"openclaw.hooks.{event}")`.

## Planned hooks

| File                    | Trigger                                                | Purpose                                                |
|-------------------------|--------------------------------------------------------|--------------------------------------------------------|
| `on_signal.py`          | Strategy generated a candidate signal                  | Allow OpenClaw to veto, scale, or annotate the signal  |
| `on_fill.py`            | An order filled                                        | Push the fill to OpenClaw's trade ledger               |
| `on_risk.py`            | Risk-limit breach (drawdown, daily loss, etc.)         | Notify OpenClaw and trigger any human pages            |
| `on_directive.py`       | OpenClaw sent a directive (memory append, halt, etc.)  | Apply the directive locally                            |
| `on_heartbeat.py`       | Periodic heartbeat                                     | Lets OpenClaw detect a dead bot                        |

## Contract

Each hook module exports a single callable:

```python
def handle(payload: dict, bridge: "OpenClawBridge") -> dict | None:
    """Return a response payload (sent back to OpenClaw) or None."""
```

Hooks must be:

- **Idempotent.** OpenClaw may retry; double-applying must be safe.
- **Non-blocking.** No hook may take >1s; offload to a queue if needed.
- **Read-mostly.** Hooks may *propose* changes (e.g. flatten positions) by
  returning a directive; they do not mutate trading state directly.

## Authoring a new hook

1. Create `openclaw/hooks/on_<event>.py`.
2. Implement `handle(payload, bridge)`.
3. Add a row to the table above.
4. Add a unit test under `tests/openclaw/hooks/test_on_<event>.py`.
