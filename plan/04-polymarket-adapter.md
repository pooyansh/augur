# Phase 4 — Polymarket adapter

**Goal:** Production-grade `ExchangeAdapter` for Polymarket. This is the first time non-throwaway code talks to a real exchange.

Use the `exchange-adapter-developer` subagent for the work in this phase — higher review bar per `CLAUDE.md`.

## Deliverables

- `src/exchanges/polymarket.py` implementing `ExchangeAdapter`.
- Paper-mode backend chosen per Phase 1's `paper-mode-decision.md` (testnet vs in-process simulator fed by live data).
- Fixture-driven unit tests using captured payloads from `scout/runs/` (Phase 1) — no network in unit tests.
- One paper-mode integration test that places, cancels, and observes a fill end-to-end.
- Wire-level details documented in `.claude/rules/05-exchanges.md` (update, don't replace).

## Hard rules

- Idempotency: the adapter accepts a deterministic id from `BaseBot` and maps it onto whatever Polymarket calls the equivalent (Phase 1 will have answered this). Retries with the same id MUST NOT double-place.
- Errors are surfaced as typed events — never raw API payloads — per `CLAUDE.md` § Exchanges.
- Wallet keys load via the sops/secrets loader from Phase 2. Never via env var or CLI flag.

## Market lifecycle

A prediction market is not a forex pair — it has a finite lifecycle and the adapter must surface every transition the bot might be mid-tick during.

- Typed events for: `MarketOpened`, `MarketClosedToTrading`, `Resolved`, `ResolutionDisputed`, `Finalized`. The strategy never has to scrape these states from REST polling.
- A market that closes to trading mid-tick: in-flight orders get a typed rejection, not a timeout. The bot's `on_tick` learns about it on the next tick via a `MarketClosedToTrading` event in `signals` (or an equivalent surface — Phase 1 will tell us which channel carries this).
- `BaseBot.place` refuses to submit into a non-tradable market and emits `warn` — defense in depth on top of the adapter check.
- **Near-resolution risk multiplier** is a per-strategy concern, not adapter-enforced. The adapter exposes `time_to_resolution`; the strategy may down-size or close as resolution approaches.

## Position reconciliation

Snapshots can lie. Fills can land during a manager restart, a network blip, or a snapshot-write failure. On every adapter startup (cold or rehydrated), and after any disconnect longer than a configured threshold:

- Pull authoritative position + open-order state from the exchange.
- Diff against the rehydrated in-memory state.
- If they agree → proceed. If they disagree → emit `warn`, write a reconciliation row to `audit_log` (referencing the prior snapshot), adopt the exchange's view as authoritative, and continue.
- If reconciliation itself fails (exchange unreachable) → adapter refuses to place new orders until reconciliation succeeds. Cancels of known-on-exchange orders are still allowed.

Reconciliation is a hard requirement before any live promotion. Skipping it is how toy bots silently double-up positions after the first crash.

## Paper-mode slippage model

Paper mode is only useful if it tells you something true about live. A `live_data → instant_fill_at_mid` simulator over-reports paper PnL on thin prediction-market books. Required model fidelity:

- Fills happen at the **far touch**, not mid, for any taking order.
- Resting orders fill only when the live book trades through the resting price; partial fills modeled at observed trade size.
- Fee schedule applied per the exchange's real fees.
- Optional latency injection: simulate a configured order placement latency so timing-sensitive strategies don't look better in paper than they will live.

The exact parameters live in the adapter and are documented in `.claude/rules/05-exchanges.md`. The point is to make paper *pessimistic*, not optimistic.

## Wallet safety (adapter-enforced)

- **On-chain allowance cap.** The hot wallet's USDC `approve()` to the exchange contract is capped at a configured working-float amount, **not** `MAX_UINT256`. The adapter performs a startup check: if the on-chain allowance exceeds the configured cap, the adapter refuses to start and emits `critical`. This is the single highest-leverage control against a hot-key compromise.
- **Hot-wallet balance ceiling.** A startup + periodic check that the hot wallet's USDC balance is within configured bounds; balance over the ceiling alerts at `warn` (treasury should be cold-wallet-side, not sitting in the hot wallet).
- **No withdrawal surface.** The adapter exposes no method that moves funds out of the exchange to an arbitrary address. If withdrawal is ever needed, it goes through a separate operator tool with a hardcoded allow-list (see Phase 6).
- **Signing isolation.** Signing happens in a single module that never logs the key, never accepts the key as a function argument from outside that module, and reads it from the secrets loader once at startup.

## Exit criteria

- [ ] All scout-phase open questions about Polymarket are now resolved in code or in `.claude/rules/05-exchanges.md`.
- [ ] Paper-mode integration test runs in CI on a schedule (not every push, but at least nightly).
- [ ] A second engineer (or `exchange-adapter-developer` subagent fresh) can read `.claude/rules/05-exchanges.md` and explain how an order flows from `OrderIntent` to a Polymarket fill without reading the adapter source.
- [ ] Reconciliation test: kill the manager mid-tick during a paper run, confirm that on restart the adapter pulls authoritative state and adopts it, with an `audit_log` row recording the diff.
- [ ] Allowance check test: deploy with an on-chain allowance > configured cap; adapter refuses to start and emits `critical`.
- [ ] Lifecycle test: paper-trade a market through `MarketClosedToTrading` and `Resolved`; the bot receives both events and exits cleanly.
