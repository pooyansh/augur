---
phase_added: 3
last_reviewed: 2026-05-09
---

# 01 — BaseBot interface and lifecycle contract

## What every strategy must implement

```python
class MyStrategy(BaseBot):
    name: ClassVar[str] = "my_strategy"       # unique registry key
    schedule: ClassVar[Schedule] = Schedule(every_seconds=900)

    async def on_tick(self, signals: SignalSnapshot) -> Decision: ...
    def snapshot(self) -> dict[str, Any]: ...
    def rehydrate(self, snapshot: dict[str, Any]) -> None: ...
```

That is the complete public surface of a strategy.  Nothing else.

## What strategies MUST NOT do

- Override `run`, `place`, `_next_client_order_id`, `_check_risk_caps`, or
  `_persist_snapshot`.
- Call `self._deps.adapter.place()` directly — always go through `self.place()`.
- Generate or fabricate `client_order_id` values — use `OrderTemplate`, not
  `OrderIntent`, when returning from `on_tick`.
- Read from or write to the DB directly — use `self._deps.state` only via the
  snapshot interface.

## Run loop (provided by BaseBot — must not be overridden)

```
1. signals  = await deps.signals.snapshot_for(config)
2.            await deps.heartbeat.beat()
3. if kill_switch.is_tripped():
       await adapter.cancel_all(market_id); continue
4. decision = await self.on_tick(signals)
5. for coid in decision.cancels: await adapter.cancel(coid)
6. for tmpl in decision.intents: await self.place(tmpl)   ← risk + audit inside
7.            await self._persist_snapshot()               ← best-effort, warn on fail
8.            sleep until next tick
```

## `place()` steps (provided by BaseBot)

```
1. Assign client_order_id from _next_client_order_id() if input is OrderTemplate.
2. Check kill switch — raise KillSwitchTripped if active.
3. Check _inflight dedup cache — return cached result if id seen.
4. Check risk caps (_check_risk_caps) — raise RiskCapExceeded if exceeded.
5. audit.write(kind="order_submitted")
6. result = await adapter.place(intent)
7. audit.write(kind="order_accepted"|"order_rejected")
8. Cache result in _inflight.
9. Return result.
```

## Deterministic `client_order_id` (invariant 2)

```python
blake2s(f"{bot_id}:{intent_seq}".encode(), digest_size=8).hexdigest()
```

`intent_seq` is a per-bot monotonic counter persisted in every snapshot.
Rehydrating a bot from a snapshot restores the counter, so the sequence
continues correctly and retries of the same logical intent replay the same id.

The adapter maps `client_order_id` to its wire field (Polymarket: derive `salt`
from it deterministically — Phase 4 documents the exact mapping in
`.claude/rules/05-exchanges.md`).

## Snapshot required keys

Every `snapshot()` dict MUST include:

| Key | Type | Meaning |
|---|---|---|
| `intent_seq` | `int` | Monotonic counter for id generation |
| `position` | `str` | Decimal-serialised net position notional |
| `last_decision_at` | `str` | ISO-8601 UTC of last `on_tick` call |

Strategy-specific keys are allowed alongside these.

## Heartbeat (v1)

`LocalHeartbeat` records last-beat time in process memory only.
Phase 5 swaps in a unix-socket heartbeat the manager process monitors.
The interface (`Heartbeat.beat()`) is unchanged across versions.

## See also

- `.claude/rules/02-state-handoff.md` — snapshot schema, rehydrate semantics,
  strategy-to-strategy handoff.
- `src/bots/base.py` — authoritative implementation.
