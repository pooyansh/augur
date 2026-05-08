# Phase 8 — Kalshi adapter (proves the abstraction)

**Goal:** Add the second exchange. The real test of `ExchangeAdapter` is whether Kalshi's REST/FIX surface fits without leaking through.

## Pre-work — repeat Phase 1 for Kalshi

Before writing any adapter code, do a Kalshi-flavored scout pass:

- Sandbox auth (Kalshi has a demo environment).
- REST shape vs FIX shape — which surface does the adapter target?
- Idempotency story: Kalshi uses `client_order_id` natively, but verify duplicate-handling semantics.
- Settlement payloads, rate limits, error categories.
- Capture findings in `learnings/kalshi-*.md` mirroring the Polymarket entries.

## Deliverables

- `src/exchanges/kalshi.py` implementing `ExchangeAdapter`.
- Fixture-driven unit tests + paper-mode integration test, matching the bar set in Phase 4.
- **Reconciliation pass:** if Kalshi exposes something `ExchangeAdapter` can't represent without an `if exchange == "kalshi"` somewhere upstream, the abstraction is wrong — revise `src/exchanges/base.py` and update Polymarket to match.
- `.claude/rules/05-exchanges.md` updated with the Kalshi half.

## Exit criteria

- [ ] A strategy can be moved from a Polymarket market to a Kalshi market by changing one line in `config/bots.yaml`.
- [ ] No `isinstance` / exchange-name branching outside `src/exchanges/`.
