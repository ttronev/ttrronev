# OpenClaw ↔ ttrronev Memory Sync

This document defines how `memory.md` (the bot's persistent memory) is kept in
sync with the OpenClaw agent's memory store. The goal: the OpenClaw agent and
the local bot always agree on facts, lessons, and limits.

## Source of truth

`memory.md` (in the repo root) is the **canonical** memory. OpenClaw mirrors
it. Conflicts are always resolved in favor of `memory.md`.

Rationale: `memory.md` is version-controlled; OpenClaw's store is not directly
diff-able by humans.

## Sync triggers

The bridge syncs in these cases:

| Trigger                                | Direction          | Frequency      |
|----------------------------------------|--------------------|----------------|
| Bot startup                            | local → OpenClaw   | once           |
| `memory.md` modified on disk           | local → OpenClaw   | within 60s     |
| OpenClaw `directives` endpoint returns a `memory.append` directive | OpenClaw → local | on receipt |
| Manual `python -m openclaw.bridge sync` | local ↔ OpenClaw  | on demand      |

## Sync protocol

### Local → OpenClaw

1. Compute SHA-256 of `memory.md`.
2. If different from the last-seen SHA, POST the full file contents to
   `{base_url}/agents/{agent_id}/memory` with header `If-Match: <last_sha>`.
3. On 412 Precondition Failed, fetch remote, three-way merge, retry once.
4. Update last-seen SHA in `.openclaw_state.json` (gitignored).

### OpenClaw → Local

OpenClaw cannot directly mutate `memory.md`. Instead it issues a directive
of the form:

```json
{
  "kind": "memory.append",
  "section": "Lessons Learned",
  "entry": "2026-05-12 — BTC/USDT — momentum_v1 — chopped out 4 times during ...",
  "rationale": "..."
}
```

The bridge's hook (`openclaw/hooks/on_directive.py`) appends the entry and
re-runs the local→remote sync to confirm.

## What's in scope for sync

- Lessons Learned section
- Strategy performance snapshot table
- Open Questions section

## What's NOT in scope for sync

- Risk limits (changed only via PR + owner approval)
- Trading universe (changed only via PR + owner approval)
- Personality (`personality.md` is never synced — it's a constitution)

## Failure mode

If OpenClaw is unreachable, the bridge runs in offline mode. Outbound events
are buffered to `logs/trades/openclaw_offline.jsonl` and replayed on next
successful connect. The bot continues to trade normally — OpenClaw is an
advisor, not a hard dependency.
