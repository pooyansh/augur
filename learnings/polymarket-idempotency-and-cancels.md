---
title: Polymarket idempotency and cancel surface
topic: polymarket
observed_on: 2026-05-09
phase: 1
status: SDK-source-only — server-side dedup behavior is the empirical question; promote after runtime probe
---

# Polymarket idempotency and cancel surface

**Provenance.** Read from `py-clob-client` source. The server-side question — does the CLOB dedup on (signature, order_hash) when the same signed payload is replayed? — is **the** open question, not answerable from source. Scout subcommands `replay-same-nonce` and `cancel-order` are built and ready to answer it.

## What I observed (from source)

### `salt` is the only thing close to a `client_order_id`, and it's random by default

```python
# python-order-utils/py_order_utils/utils.py
def generate_seed() -> int:
    """Pseudo random seed"""
    now = datetime.now().replace(tzinfo=timezone.utc).timestamp()
    return round(now * random())

# python-order-utils/py_order_utils/builders/order_builder.py
def build_order(self, data: OrderData) -> Order:
    return Order(
        salt=int(self.salt_generator()),   # called per build_order()
        maker=normalize_address(data.maker),
        ...
    )
```

Critical implications:

1. **`salt` is not a cryptographic random** — it's `epoch_seconds_float * random.random()`. Collisions are improbable but possible. For a deterministic `BaseBot.place` retry path, treat `salt` as **caller-supplied**, not auto-generated.
2. **`salt` is part of the EIP-712 signed payload** (top field of the `Order` struct). Two orders with the same `(salt, maker, signer, tokenId, makerAmount, takerAmount, expiration, nonce, feeRateBps, side, signatureType)` produce **identical signatures** — the server can dedup on the signature/order-hash if it chooses to. Whether it does is the empirical question.
3. **The `nonce` field is NOT a per-order idempotency key.** Source doc-comment: *"Nonce used for onchain cancellations"*. It belongs to a different mechanism — bumping it cancels all prior orders sharing that nonce on-chain. Per-order, leave at `0`.
4. **There is no `x-poly-client-id` HTTP header.** The auth header set is exactly `POLY_ADDRESS / POLY_SIGNATURE / POLY_TIMESTAMP / POLY_API_KEY / POLY_PASSPHRASE` (verified in `py-clob-client/py_clob_client/headers/headers.py`). No client-side request id is transmitted.

### How `BaseBot` should map its `client_order_id` → Polymarket

Two viable approaches, picked at adapter level:

**Option A — Cache the SignedOrder.** On first construction of an `OrderIntent`, the adapter builds and signs the order, then caches `(client_order_id → SignedOrder)`. Retries replay the **exact same signed payload** to the CLOB. Server dedup on order hash is the safety net.

**Option B — Deterministic salt injection.** Override `OrderBuilder(salt_generator=lambda: deterministic_int_from_client_order_id)`. Reconstructing the order on retry produces an identical signature without caching the signed payload — useful if process memory was lost (snapshot/rehydrate). Salt collision risk is now correlated with `client_order_id` collisions, which `BaseBot` already avoids by construction.

**Decision deferred to Phase 4.** Option B is more elegant if `salt` is acceptable as a UInt256 derived from the `client_order_id`. Option A works regardless. The empirical probe (`replay-same-nonce`) will tell us whether the server dedups silently, dedups loudly (with an error code), or duplicates — that result drives the choice.

### Cancel surface — four methods, all REST DELETE, all L2-auth (no EIP-712 signed cancels)

From `py-clob-client/py_clob_client/client.py` (lines ~663-748) and `endpoints.py`:

| SDK method | HTTP | Path | Cancels |
|---|---|---|---|
| `cancel(order_id)` | DELETE | `/order` | Single by id |
| `cancel_orders(order_ids: list)` | DELETE | `/orders` | Batch by ids |
| `cancel_all()` | DELETE | `/cancel-all` | Every open order for the wallet |
| `cancel_market_orders(market="", asset_id="")` | DELETE | `/cancel-market-orders` | All orders in a market or for one outcome |

Plus `get_orders(params, next_cursor="MA==")` → `GET /data/orders` to query open orders.

**No EIP-712 signed cancel.** All four cancel calls are unsigned REST with HMAC-only L2 auth. This is structurally different from some on-chain CLOBs (e.g. dYdX) where cancels are signed messages — Polymarket trusts the L2 HMAC.

### Cancel idempotency — open question

If you cancel the same `order_id` twice, the second call's response shape is the empirical question. Three plausible outcomes:

- 200 with a "no-op" body
- 4xx with `{"error": "order not found"}` (matches the read-only `probe-errors` shape)
- 4xx with a distinct "already cancelled" code

The scout's `cancel-order` subcommand sends the cancel twice on purpose; the second response is the data point.

## Why it matters

- Mapping a deterministic `client_order_id` (BaseBot invariant 2) onto Polymarket cleanly is **not free** — there's no native field. Adapter must take Option A or Option B; both work, but the retry semantics around snapshot/rehydrate diverge.
- The lack of a client request-id header means the **only** evidence that two HTTP attempts represent the same logical order is the EIP-712 signature itself. Any retry path must reconstruct an identical SignedOrder; perturbations (timestamp differences in the signed payload, etc.) will surface as duplicates.
- Cancel-by-market is available — useful for the kill-switch path (Phase 6). Killing a bot can issue one `cancel_market_orders(market=…)` per affected market instead of N per-order cancels.

## Implications for the plan

- **Phase 4 (`plan/04-polymarket-adapter.md`):**
  - Decide Option A vs B for `client_order_id → salt` mapping after the empirical probe. Document the choice in `.claude/rules/05-exchanges.md`.
  - Adapter cancel API exposes (single, batch, all, by-market) one-to-one with the SDK methods.
  - Reconciliation (Phase 4): `get_orders` is the authoritative-state read.
- **Phase 6 (`plan/06-risk-and-alerting.md`):**
  - Kill switch's "cancels open orders across all bots" is implemented as `cancel_market_orders(market=condition_id)` per affected market, not a sweep over individual orders. Faster, fewer round trips, less rate-limit pressure when it matters most.

## Evidence

- `python-order-utils/py_order_utils/utils.py` — `generate_seed`.
- `python-order-utils/py_order_utils/builders/order_builder.py` — salt injection point (`salt_generator` constructor arg).
- `py-clob-client/py_clob_client/client.py` lines ~663-748 — cancel methods.
- `py-clob-client/py_clob_client/endpoints.py` — endpoint constants.
- `py-clob-client/py_clob_client/headers/headers.py` — auth header set; no client-id.
- `scout/polymarket_scout.py` — `cmd_replay_same_nonce`, `cmd_cancel_order` (built; not yet run).

## Open follow-ups (the empirical probes)

1. Replay an identical SignedOrder to the CLOB twice. Does the server return the same `order_id` (silent dedup), a `409`/error (loud dedup), or two distinct order ids (no dedup)? — `replay-same-nonce`.
2. Cancel the same `order_id` twice. What is the second response shape? — `cancel-order`.
3. `cancel_market_orders` behavior when there are zero open orders in the market — empty list vs error. — additional probe to add.
4. Whether `nonce > 0` cancellation actually invalidates orders signed with the prior nonce — out of Phase 1 scope (on-chain interaction); flag for Phase 4 if the kill-switch wants this lever.
