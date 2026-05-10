---
phase_added: 3
last_reviewed: 2026-05-09
---

# 07 — Testing tiers and requirements

## Three tiers

| Tier | Location | DB required | When mandatory |
|---|---|---|---|
| Unit / property | `tests/unit/` | No | Always (every PR) |
| Integration | `tests/integration/` | Optional (skipped if `PG_DSN` unset) | Before paper promotion |
| Paper | Live signals, simulated fills | Exchange connectivity | Before live promotion (minimum window: TODO Phase 7) |

## Hypothesis — mandatory on order/state machinery

Every module touching `BaseBot.place()`, `client_order_id` generation, risk
caps, kill-switch logic, or snapshot/rehydrate MUST have a Hypothesis property
suite.  Running `pytest tests/unit/ -q` must include Hypothesis tests.

### Canonical invariants (implemented in Phase 3)

Located in `tests/unit/test_base_bot_place.py`:

1. **Idempotency under retry.** For any `OrderIntent` retried N times with the
   same `client_order_id`, `audit_log` records exactly one accepted entry.
   The `_inflight` dedup cache is the mechanism; the adapter sees exactly one
   call.

2. **Risk caps always enforced.** Generated `(price, size)` pairs that exceed
   any cap (`max_position_notional`, `max_daily_loss`, `max_orders_per_minute`)
   always raise `RiskCapExceeded` and produce zero adapter calls.

3. **Kill switch is absolute.** When `KillSwitchReader.is_tripped() = True`,
   `place()` raises `KillSwitchTripped` regardless of intent content; zero
   adapter calls observed across all generated intents.

## Integration test pattern

`tests/integration/test_snapshot_rehydrate.py`:

- Default path: uses `InMemoryStateRepository` — no Postgres needed.
- `@pytest.mark.integration` path: uses real Postgres via `PG_DSN` env var;
  skipped if unset.  CI sets `PG_DSN` in the integration job.

## Paper promotion minimum window

TODO — defined in Phase 7 alongside the first real strategy.  Likely: minimum
5 days of paper trading with < N% simulated fill deviation before promotion to
live is permitted.

## Fixtures

Located in `tests/fixtures/`:

| Module | Purpose |
|---|---|
| `echo_exchange.py` | `EchoExchange` — in-memory adapter; configurable accept/reject/fill |
| `null_strategy.py` | `NullStrategy` — no-op bot for lifecycle tests; `make_null_bot` factory |
| `clocks.py` | `ManualClock` — controllable time without sleeping |
| `state.py` | `InMemoryStateRepository`, `InMemoryKillSwitch`, `InMemoryAuditLogger` |

All unit tests and Hypothesis suites MUST use these fakes — never a real DB or
exchange in the `tests/unit/` tier.
