# Phase 7 — First real strategy (`momentum_v1`)

**Goal:** Run a single end-to-end strategy in paper mode against a real Polymarket market for the minimum paper duration defined in `.claude/rules/07-testing.md`.

## Deliverables

- `src/bots/momentum/strategy.py` — `momentum_v1` per `CLAUDE.md` example. Subscribes to a 15-min BTC signal, decides on a single Polymarket BTC market.
- `src/signals/btc_15min.py` — 15-min BTC sampler (Coingecko or Binance — pick whichever Phase 1 didn't burn rate-limit on).
- Backtest harness in `tests/strategies/test_momentum_v1.py` replaying historical BTC + book snapshots through `on_tick`.
- Paper-mode run on the live system for the minimum window.
- Promotion checklist (`/deploy-bot`) drafted and reviewed but **not executed** — live promotion is Phase 9.

## Exit criteria

- [ ] Strategy survives N paper days without manual intervention.
- [ ] Snapshot/rehydrate verified during a deliberate manager restart mid-run.
- [ ] Audit log shows every intent + result; sums match observed paper PnL.
