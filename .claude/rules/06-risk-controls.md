# Rule 06 — Risk Controls

## Cap rationale

All three caps are enforced in `BaseBot.place()` via `check_caps()` in
`src/risk/caps.py`.  Strategies **cannot bypass them** (invariant 3).

| Cap                      | What it limits                              | Rationale                                         |
|--------------------------|---------------------------------------------|---------------------------------------------------|
| `max_position_notional`  | Open position in collateral units           | Prevents unlimited exposure from runaway strategy |
| `max_daily_loss`         | Cumulative P&L loss today (UTC midnight)    | Hard stop-loss per bot per trading day            |
| `max_orders_per_minute`  | Orders in any rolling 60-second window      | Burst protection; prevents rate-limit hammering   |

All three caps are **mandatory** in every `BotEntry.risk` block.  There is no
implicit global default — every bot must declare its risk tolerance explicitly.

## Kill-switch semantics

The global kill switch lives in Postgres (`kill_switch` table, row id=1).
`KillSwitchReader` caches the DB read for ~1 second.

### Trigger points

1. **`BaseBot.place()`** — checks `is_tripped()` before every order.
   Raises `KillSwitchTripped` if active; no order reaches the adapter.
2. **`BaseBot.run()`** — at the top of every tick, checks `is_tripped()`.
   If tripped, skips `on_tick` entirely.

### Cascade semantics

When `run()` detects a tripped switch:

1. `KillSwitchCascade.on_trip(adapter, market_id)` is called.
2. On the **first** tripped tick: issues `adapter.cancel_all(market_id)` once.
3. Subsequent tripped ticks: cascade flag is set; **no duplicate `cancel_all`**.
4. Cancel failure: logged at `warning`; an audit row is written with
   `kind="kill_switch_cancel_failed"`.  The kill switch is still honoured
   (no orders will be placed regardless).
5. When the switch is **untripped**: `cascade.reset()` is called so the next
   trip re-issues `cancel_all`.

### Hot-wallet balance check (Phase 4 forward-link)

`KillSwitchWriter.trip()` is called automatically when a Phase 4 balance check
fails (allowance exhausted or balance below minimum).  This freezes placement
across all bots.  Detail in Phase 4 adapter spec.

## Audit-log invariants

The `audit_log` table is **append-only** (invariant 5):
- No `UPDATE` or `DELETE` operations are ever issued by application code.
- The DB role used by the application has no `UPDATE`/`DELETE` privileges on
  `audit_log` (enforced at the DB schema level — see `src/state/models.py`).
- **Corrections are new rows** that reference the original via
  `payload["references_audit_id"]`.  The original row is never modified.
- `AuditLogger.write()` only ever calls `session.add(AuditLog(...))`.

## Withdrawal allow-list

- `WithdrawalAllowlist` (`src/risk/withdrawal_allowlist.py`) is loaded once at
  startup from `/run/secrets/withdrawal_allowlist.yaml`.
- **Strategies have no withdrawal code path.**  Only operator tools may move
  funds off-exchange, and they MUST call `is_allowed(address)` before doing so.
- Missing file → empty list → **all withdrawals refused** (fail-closed).
- Address matching is case-insensitive (`.lower()` normalisation).
- Schema:
  ```yaml
  addresses:
    - "0xABCDEF..."
    - "0x123456..."
  ```

## Paper-mode default

Every new strategy runs in `mode: paper` until explicitly promoted via the
three-lock rule (invariant 1).  Paper mode uses `EchoAdapter` or equivalent
in-memory simulator.  Real money is never at risk in paper mode.
