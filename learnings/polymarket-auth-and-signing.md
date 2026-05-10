---
title: Polymarket auth and signing — order field set, auth tiers
topic: polymarket
observed_on: 2026-05-09
phase: 1
status: SDK-source-only — promote to current after a runtime probe with a funded wallet
---

# Polymarket auth and signing

**Provenance.** Read from `py-clob-client` and `python-order-utils` source on GitHub (commit/tag inferred latest as of 2026-05). Not yet empirically confirmed against a live CLOB — the scout's `place-resting-order` subcommand is built but unrun (blocked on Phase 1 task #8 wallet provisioning). Promote `status: current` after the first successful signed-order round-trip.

## What I observed (from source)

### Two auth tiers

- **L1** — Polygon EOA private key. Signs the EIP-712 typed-data order. The wallet must hold MATIC for gas-on-chain interactions (allowance approval, on-chain cancellation) and USDC.e for collateral.
- **L2** — derived API credentials `(api_key, secret, passphrase)` used to authenticate REST calls via HMAC-signed headers `POLY_ADDRESS / POLY_SIGNATURE / POLY_TIMESTAMP / POLY_API_KEY / POLY_PASSPHRASE`. Derived from an L1 signature once via `derive_api_key()` / `create_api_key()`; used per-request thereafter.

The order itself is L1-signed (EIP-712); the REST envelope it rides in is L2-authenticated (HMAC). Two distinct signature mechanisms, two distinct keys.

### Wallet types

`signature_type` (`uint8`) selects how the maker is authenticated:

- `0` — `EOA`: signer == maker, plain EOA.
- `1` — `POLY_GNOSIS_SAFE`: maker is a Gnosis-Safe-style proxy (Polymarket UI wallet for new users since the proxy-wallet rollout).
- `2` — `POLY_PROXY`: legacy Polymarket proxy.

**Mismatch between configured `signature_type` and the actual wallet shape causes silent rejection** — flagged in research, the scout's `bad-orders` probe will surface the exact rejection format.

### EIP-712 `Order` struct — exact field set (signed)

Source: `python-order-utils/py_order_utils/model/order.py`. **Field order matters for the hash**:

```python
class Order(EIP712Struct):
    salt          = Uint(256)
    maker         = Address()
    signer        = Address()
    taker         = Address()
    tokenId       = Uint(256)
    makerAmount   = Uint(256)
    takerAmount   = Uint(256)
    expiration    = Uint(256)
    nonce         = Uint(256)
    feeRateBps    = Uint(256)
    side          = Uint(8)
    signatureType = Uint(8)
```

**12 fields total.** The wire envelope appended to this for transmission (`SignedOrder.dict()`) adds `signature` (hex bytes from EIP-712 sign).

### Caller-supplied vs derived

What the strategy / `OrderIntent` must specify:

| Field | Source |
|---|---|
| `tokenId` | strategy (the outcome being traded) |
| `makerAmount`, `takerAmount` | strategy (price × size; see below for who multiplies) |
| `side` (0=BUY, 1=SELL) | strategy |
| `feeRateBps` | strategy (per-market; read from CLOB market info) |
| `expiration` | strategy (default `0` = no expiry; 0 is acceptable) |
| `nonce` | strategy (`0` for normal orders; non-zero is for on-chain cancellation, **not** per-order idempotency) |
| `signatureType` | adapter (per-wallet config) |

What the SDK derives:

| Field | How |
|---|---|
| `salt` | random per `build_order()` call (see `polymarket-idempotency-and-cancels.md`) |
| `maker` | `funder` address (defaults to signer for EOA) |
| `signer` | EOA address (= maker for EOA wallets) |
| `taker` | `0x000…0` for public orders |

`makerAmount` / `takerAmount` are integer USDC-base-unit amounts (6 decimals). The SDK's `OrderArgs → OrderData` conversion does the price-and-size math:
- BUY: `makerAmount = round(price * size * 1e6)` USDC, `takerAmount = round(size * 1e6)` shares.
- SELL: inverted.

Strategies must be aware of rounding to `minimum_tick_size` and `minimum_order_size` — both fields live only on the **CLOB** market response (`learnings/polymarket-api-shape.md`).

### Note on the API key path

`py-clob-client.derive_api_key()` is idempotent on the server side — calling it twice on the same EOA returns the same `api_key`. The scout's `derive-api-keys` subcommand can be re-run safely.

## Why it matters

- The `OrderIntent` shape inside `BaseBot` must carry **at minimum**: `(token_id, side, price, size, fee_rate_bps, expiration_default=0)`. Everything else (`maker`, `signer`, `taker`, `salt`, `signatureType`) is adapter-derived and must not leak into strategy code.
- Two distinct keys (L1 EOA + L2 API creds) means the secrets file (`secrets/exchanges.enc.yaml`) must hold both. Loss of L2 alone is recoverable (re-derive); loss of L1 is unrecoverable (rotate via cold-wallet sweep + new EOA).
- Wallet type misconfiguration is a silent failure mode — any production adapter must verify on startup that `signature_type` matches the actual on-chain wallet shape before placing the first order.

## Implications for the plan

- **Phase 4 (`plan/04-polymarket-adapter.md`):**
  - `OrderIntent` fields locked to the minimum set above; document in `.claude/rules/05-exchanges.md`.
  - Adapter startup check: derive (or load) L2 keys, verify `signature_type` against the on-chain wallet type, refuse to start on mismatch — this joins the existing on-chain allowance check.
  - Two-tier secret loader: L1 keyfile → tmpfs (Phase 2); L2 derived keys cached at adapter startup, regenerable from L1.
- **Phase 1 exit criterion** "minimum OrderIntent fields" → answered above (caller fields list); see also `polymarket-idempotency-and-cancels.md` for the salt question.

## Evidence

- `py-clob-client/py_clob_client/order_builder/builder.py` `create_order()` — caller-supplied `OrderArgs → OrderData` mapping.
- `py-clob-client/py_clob_client/headers/headers.py` — L2 HMAC header construction; **no `x-poly-client-id` header exists**.
- `python-order-utils/py_order_utils/model/order.py` — `Order` EIP712Struct.
- `python-order-utils/py_order_utils/builders/order_builder.py` — order construction; `salt_generator` injection point.
- `scout/polymarket_scout.py` — `cmd_place_resting_order`, `cmd_derive_api_keys`, `cmd_init_wallet` (built; not yet run).

## Open follow-ups

- Confirm `signature_type` mismatch produces a server-side rejection (which status / body shape?). `bad-orders` probe will surface this.
- Confirm `expiration=0` is accepted by the CLOB or whether a non-zero expiry is required for resting orders. Also verifiable via `bad-orders`.
- Capture the wire format of the request to `/order` POST (the SDK constructs the JSON body; we want the bytes).
