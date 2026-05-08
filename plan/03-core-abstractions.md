# Phase 3 — Core abstractions

**Goal:** Translate Phase 1 findings into the typed contracts the rest of the system codes against. This is where the abstractions get committed; if Phase 1 findings change, this phase reopens.

## Deliverables

- `src/bots/base.py` — `BaseBot` per `CLAUDE.md` § Bot model. Provides `run`, `place`, heartbeat, snapshot scheduling. Abstract: `on_tick`, `snapshot`, `rehydrate`.
- `src/exchanges/base.py` — `ExchangeAdapter` ABC with the surfaces Phase 1 proved necessary. Typed events for fills, cancels, rejections, settlements. Mode-aware (`live` / `paper`).
- `src/signals/base.py` — `Signal` ABC + the shared cache + scheduler so N bots subscribed to the same feed produce 1 upstream call. Staleness `warn` hook.
- `src/state/` — SQLAlchemy models for `bot_state`, `audit_log`, `kill_switch`, `market_history` view. Snapshot read/write helpers. Migration in Alembic.
- `src/manager/registry.py` — strategy auto-discovery via entry points from `src/bots/*/strategy.py`.
- `src/secrets/` — sops loader and the **redaction filter** that masks any value matching a loaded secret. Wired into the root logger by default.
- Property-based tests (Hypothesis) on `BaseBot.place` for: idempotency under retries, risk-cap enforcement, kill-switch interception. Per `CLAUDE.md`: Hypothesis is mandatory on order/state machinery.

## Design constraints to preserve

- `client_order_id` generation lives in `BaseBot`, never in strategies or adapters (invariant 2).
- Risk checks live in `BaseBot.place`, never in strategies (invariant 3).
- Snapshot writes are best-effort; failure → `warn` alert, tick continues (invariant 6).

## Exit criteria

- [ ] A trivial in-memory `EchoExchange` adapter exists for tests and a no-op `NullStrategy` bot can run end-to-end through `BaseBot.run` against it.
- [ ] Snapshot → kill process → rehydrate from DB reproduces in-memory state in an integration test.
- [ ] Hypothesis suite is green and covers idempotency, caps, kill switch.
