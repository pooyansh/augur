---
title: Polymarket API shape — endpoints, identifiers, payloads
topic: polymarket
observed_on: 2026-05-08
phase: 1
status: current
---

# Polymarket API shape

## What I observed

Five subcommands of `scout/polymarket_scout.py` hit production Polymarket from a US/dev IP on 2026-05-08. No auth, all read-only. Outputs in `scout/runs/`.

### Endpoints (confirmed reachable)

| Service | URL | Style |
|---|---|---|
| CLOB REST | `https://clob.polymarket.com` | snake_case fields |
| Gamma metadata | `https://gamma-api.polymarket.com` | camelCase fields |
| WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | mixed shapes (see below) |

Both REST hosts respond fine with no auth for **public read** endpoints — cold-start latency was ~3.8s for the first Gamma call (TLS), then 178ms for a subsequent CLOB orderbook fetch on the same process.

### Identifier model (canonical)

| Field | Type | Where used |
|---|---|---|
| `condition_id` (CLOB) / `conditionId` (Gamma) | hex bytes32 string | the **market** identifier; what the adapter persists per bot |
| `token_id` / `asset_id` / `clobTokenIds[i]` | huge decimal string (ERC-1155 position id) | the **outcome** identifier; what you order against; one per outcome (binary = 2) |
| `slug` | string | human-readable, Gamma-only; **not** stable, do not use as id |
| `id` (Gamma) | numeric string | Gamma-internal, ignore |
| `questionID` / `question_id` | hex bytes32 | UMA oracle id, useful for resolution context only |

A bot that says "I trade `russia-ukraine-ceasefire-before-gta-vi-554` YES" persists `(condition_id, token_id_for_yes)` — both are needed. The token_id by itself doesn't tell you which outcome.

### Field-encoding gotcha (high-impact)

Gamma returns several list-shaped fields as **JSON-encoded strings**, not arrays:
```python
m['clobTokenIds']    # '["8501...", "2527..."]'   <-- string!
m['outcomes']        # '["Yes", "No"]'
m['outcomePrices']   # '["0.545", "0.455"]'
m['umaResolutionStatuses']  # '[]'
```
The CLOB's `/markets/{condition_id}` endpoint exposes the equivalent as proper objects in `tokens: [{token_id, outcome, price, winner}, ...]`. **Prefer the CLOB shape; only fall back to Gamma when CLOB doesn't carry the field.**

### Schema diff: CLOB vs Gamma on the same market

22 fields exist only in the CLOB response, 83 only in Gamma. The most important splits:

- **CLOB-only:** `tokens` (array), `minimum_tick_size`, `minimum_order_size`, `neg_risk`, `neg_risk_market_id`, `neg_risk_request_id`, `rewards`, `tags`, `seconds_delay`, `is_50_50_outcome`, `fpmm`. **Tick size and minimum order size live only in CLOB**, so the adapter must hit CLOB to size orders correctly.
- **Gamma-only:** all volume/liquidity/price-change rollups (`volume24hr`, `oneDayPriceChange`, `lastTradePrice`, `bestBid`, `bestAsk`, `spread`), `feeSchedule`, `events`, `acceptingOrders`, all UI metadata.

`bestBid`/`bestAsk`/`spread` from Gamma matched the CLOB orderbook within the same minute — Gamma is acceptable for low-frequency price reads.

### Fees (from `feeSchedule` on this market)

```json
{"exponent": 1, "rate": 0.05, "takerOnly": true, "rebateRate": 0.25}
```
5% taker fee, 25% maker rebate, no maker fee otherwise. Fee schedule varies per market — read it, do not hardcode.

### Orderbook shape (`GET /book?token_id=...`)

```json
{
  "market": "<condition_id>",
  "asset_id": "<token_id>",
  "timestamp": "...",
  "hash": "...",
  "bids": [{"price": "0.54", "size": "451.94"}, ...],   // strings, not numbers
  "asks": [{"price": "0.55", "size": "1717.05"}, ...]
}
```

Prices and sizes are decimal **strings**. Use `Decimal`, not `float`. Top of book in this sample: bid=0.54 ask=0.55 spread=0.01.

### WS shapes (two distinct, only one has `event_type`)

On subscribe (`{"type":"market","assets_ids":["<token_id>"]}`), the server emits:

1. **Initial snapshot** — top-level **list** of objects, **no `event_type`** field:
   ```json
   [{"market": "...", "asset_id": "...", "timestamp": "...", "hash": "...",
     "bids": [...], "asks": [...]}]
   ```
   Notably the snapshot for one subscribed `asset_id` may include the **mirror outcome's** book in the same list (binary YES+NO arrived together). Confirm before assuming.

2. **Update** — top-level **object** with `event_type`:
   ```json
   {"event_type": "price_change",
    "market": "<condition_id>",
    "timestamp": "...",
    "price_changes": [
      {"asset_id": "...", "price": "0.13", "size": "400.76",
       "side": "BUY", "hash": "...", "best_bid": "0.54", "best_ask": "0.55"},
      ...
    ]}
   ```
   `best_bid`/`best_ask` are pre-computed and embedded in each entry — adapter does not need to maintain a full book to track top-of-book.

Other event types (`book`, `tick_size_change`, `last_trade_price`) likely exist but were not observed in this 15-second sample on a low-volume market.

### Resolution payload (`find-resolved`)

A resolved market exposes:
- `closed: true`
- `outcomePrices: '["1","0"]'` (winner is 1.0, loser is 0.0; encoded string per the gotcha above)
- `umaResolutionStatus: "resolved"` (Gamma) — singular, distinct from `umaResolutionStatuses` (plural list)
- `resolutionSource` is often empty even on resolved markets — do not rely on it

Recently-resolved query: `GET /markets?closed=true&order=endDate&ascending=false`. Caveat: `endDate` on these is sometimes a sentinel like `2028-01-01T05:00:00Z` for markets that resolved early via UMA; ordering by `endDate` is **not** "ordering by resolution time."

## Why it matters

- Adapter must hit **both** CLOB (sizing fields) and Gamma (UI rollups, fees) — neither alone is enough. Decision deferred to Phase 4 design.
- The string-encoded JSON arrays in Gamma are a top source of latent bugs; a typed parser must `json.loads()` those fields before downstream code touches them.
- WS has two distinct top-level shapes (list snapshot vs object update). The adapter's WS dispatcher must branch on `isinstance(payload, list)`, not on `event_type` (snapshots have none).
- All numeric quantities arrive as strings. `Decimal` is mandatory; `float` will accumulate error in PnL calculations.

## Implications for the plan

- **Phase 4 (`plan/04-polymarket-adapter.md`):**
  - `OrderIntent` must carry both `condition_id` and `token_id`; the helper that builds it from a market id has to expand to two ids.
  - Tick size and min order size come from CLOB `/markets/{condition_id}.tokens[i]` (or `minimum_tick_size`); add a startup cache.
  - WS dispatcher must handle list-snapshot vs object-update. Add a fixture per shape from the captured `runs/` payloads.
- **Phase 1 (`plan/01-exploratory-bot.md`):** open question "What is the canonical market identifier?" → **answer: `(condition_id, token_id)` pair.** The condition_id alone is not enough.

## Evidence

- `scout/runs/20260508T034227Z-list-markets.jsonl` — 5 markets, full Gamma schema.
- `scout/runs/20260508T034334Z-get-market.jsonl` — same market via CLOB + Gamma, schema diff in stdout output.
- `scout/runs/20260508T034439Z-get-orderbook.jsonl` — top-5 book + 178ms latency.
- `scout/runs/20260508T034501Z-watch-stream.jsonl` — 5 messages, both WS shapes.
- `scout/runs/20260508T034440Z-find-resolved.jsonl` — 5 resolved markets.

## Open follow-ups

- WS message types beyond `price_change` (e.g. `book`, `tick_size_change`, `last_trade_price`) — capture on a higher-volume market.
- Whether the WS reconnect resumes from a sequence id or only re-snapshots — relevant for order-book consistency.
- `neg_risk: true` markets — order signing uses a different exchange contract; not yet observed in the wild from the scout. Capture one before Phase 4.
