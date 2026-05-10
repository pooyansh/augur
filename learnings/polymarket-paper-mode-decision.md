---
title: Polymarket paper-mode backend — tentative decision
topic: polymarket
observed_on: 2026-05-08
phase: 1
status: tentative — confirm after auth/signing probes complete
---

# Polymarket paper-mode backend — tentative decision

## Decision (tentative)

**Paper mode = in-process simulator fed by live mainnet data.** Not testnet.

## What I observed

The decision rests on three facts established in Phase 1 read-only probes plus one fact that is research-only and needs scout confirmation:

1. **Live mainnet is fully readable without auth** (`learnings/polymarket-api-shape.md`). Orderbooks (178 ms warm), Gamma metadata, and WS book updates all work from a US/dev IP without credentials. Paper mode can ingest the same live data the live adapter would consume — no parallel data plane needed.

2. **`/trades` is auth-gated** (`learnings/polymarket-errors-and-rate-limits.md`). Public REST does not expose recent prints. For a fill simulator, the trade tape comes from the WS `last_trade_price` event channel (which arrives unauth) rather than REST.

3. **WS is sufficient for a fill simulator.** Snapshot-on-subscribe + `price_change` deltas with embedded `best_bid`/`best_ask` give the simulator everything needed to model: top-of-book takes, resting-order fills (book trades through resting price), and partial fills at observed trade size — the requirements stated in `plan/04-polymarket-adapter.md` § "Paper-mode slippage model."

4. **(Research-only, unconfirmed)** Polymarket has historically run a Mumbai → Amoy testnet, but the research brief flagged it as "sporadic uptime, low liquidity" with `chain_id=80002` deprecated/migrated multiple times in the last 18 months. I have **not** independently confirmed any usable testnet exists in 2026-05; that requires the auth/signing probes (currently blocked on wallet provisioning).

## Why it matters

A choice between testnet and in-process simulator drives the Phase 4 paper-mode implementation. Two very different code paths:

- **Testnet path:** the adapter has a `mode` switch that flips the host between `clob.polymarket.com` and a testnet host; otherwise the same code runs. Fills are real-on-testnet. Easier to build, harder to trust if testnet diverges from mainnet behavior or has thin/empty books.
- **In-process simulator path:** the adapter consumes live mainnet data but routes orders through a local matching simulator that never transmits. Harder to build correctly (slippage model, partial fills, latency injection per Phase 4). Easier to trust because it consumes the same data we'll trade against.

## Why "in-process simulator" is the right tentative call

- Even if testnet exists and works, its books are thin and its matching dynamics aren't ours. Strategies that paper well on testnet but fail in mainnet are a documented industry pattern; the paper-mode goal is to be **pessimistic about live**, not optimistic.
- The Phase 4 paper-mode requirements (`fills at far touch, partial fills sized to observed trade size, real fees, latency injection`) are the simulator's spec already. Building those costs effort either way; testnet doesn't save us any of it.
- The adapter's mode switch can still expose `paper-testnet` as a third mode later for adapter-level integration tests, without giving up the in-process simulator as the default.

## Implications for the plan

- **`plan/04-polymarket-adapter.md`:** carry the simulator as the canonical paper backend. Drop "testnet vs in-process simulator" framing; the choice is made (tentatively).
- **Auth/signing probes (Phase 1, blocked):** when the wallet is in hand, do **one** Amoy-or-equivalent probe to confirm whether testnet exists and is usable; promote this learning to `status: current` if confirmed and adjust the call. If testnet is dead, promote and we keep the simulator decision permanently.
- **Phase 4 reconciliation:** in-process simulator means **there is no exchange-side state** to reconcile against in paper mode — the simulator's state is authoritative. Reconciliation logic still ships, but its paper-mode test path is trivial. Live-mode reconciliation is unchanged and still mandatory.

## Evidence

- All Polymarket scout runs in `scout/runs/` from 2026-05-08T03:42–03:45 UTC.
- Phase 4 paper-mode requirements: `plan/04-polymarket-adapter.md` § "Paper-mode slippage model".

## Open follow-ups

- Confirm/refute usable testnet during the auth/signing scout pass. This learning's `status` flips to `current` (or revises to "testnet usable") at that point.
- Decide the trade-tape source for the simulator: WS `last_trade_price` is the cheapest; subgraph queries are richer but slower. Phase 4 design call.
