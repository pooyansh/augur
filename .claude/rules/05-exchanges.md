# Rule 05 — Exchange Adapters

## Adapter contract

All adapters implement `ExchangeAdapter` from `src/exchanges/base.py`.

```python
class ExchangeAdapter(ABC):
    venue: ClassVar[str]
    async def place(self, intent: OrderIntent) -> OrderResult: ...
    async def cancel(self, client_order_id: str) -> bool: ...
    async def cancel_all(self, market_id: str | None = None) -> int: ...
    async def get_market(self, market_id: str) -> Market: ...
    def events(self) -> AsyncIterator[ExchangeEvent]: ...
```

Strategies never see wire formats. The adapter translates to/from the typed
structures in `src/exchanges/base.py` exclusively.

---

## Polymarket — Wire-Level Details

### Identifier model

| Concept | Field | Example |
|---|---|---|
| Market | `condition_id` | `0xbd31dc8a...` (hex string) |
| Outcome token | `token_id` | `71321045...` (decimal ERC-1155 token ID) |

A single prediction market has one `condition_id` and two token IDs (YES, NO).
The bot trades a specific outcome token. The `Market` dataclass carries both:
`market_id = condition_id`, `token_id = the outcome token`.

### Two authentication tiers

| Tier | Purpose | Mechanism |
|---|---|---|
| L1 | Order signing (on-chain validity) | EIP-712 typed-data signed with wallet private key |
| L2 | REST API authentication | HMAC-SHA256 headers |

**L2 headers** (all required on authenticated endpoints):
- `POLY_ADDRESS` — checksummed wallet address
- `POLY_SIGNATURE` — base64(HMAC-SHA256(timestamp + METHOD + path + body, l2_secret))
- `POLY_TIMESTAMP` — Unix milliseconds as string
- `POLY_API_KEY` — L2 API key
- `POLY_PASSPHRASE` — L2 passphrase

The `SigningModule` in `src/exchanges/polymarket_signing.py` encapsulates both tiers.
The L1 private key is loaded once at startup and never re-exposed.

### EIP-712 domain

```python
domain = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": 137,  # Polygon mainnet
    "verifyingContract": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    # TODO: verify this address against the live CTF Exchange contract before live trading
}
```

### EIP-712 Order struct (12 fields)

| Field | Type | Notes |
|---|---|---|
| `salt` | `uint256` | Deterministic nonce from `client_order_id` |
| `maker` | `address` | Wallet address |
| `signer` | `address` | Same as maker for EOA |
| `taker` | `address` | Zero address for open orders |
| `tokenId` | `uint256` | ERC-1155 outcome token ID |
| `makerAmount` | `uint256` | USDC micro-units (6 dec) for BUY; share micro-units for SELL |
| `takerAmount` | `uint256` | Share micro-units for BUY; USDC micro-units for SELL |
| `expiration` | `uint256` | 0 for GTC orders |
| `nonce` | `uint256` | 0 for standard orders |
| `feeRateBps` | `uint256` | Fee rate in basis points |
| `side` | `uint8` | 0 = BUY, 1 = SELL |
| `signatureType` | `uint8` | 0 = EOA, 1 = proxy, 2 = Gnosis Safe |

**Amount encoding:**
```
BUY:  makerAmount = round(price * size * 1_000_000)   # USDC the maker spends
      takerAmount = round(size * 1_000_000)            # shares the maker receives

SELL: makerAmount = round(size * 1_000_000)            # shares the maker spends
      takerAmount = round(price * size * 1_000_000)    # USDC the maker receives
```

### `client_order_id → salt` mapping (Option B — deterministic)

```python
salt = int.from_bytes(bytes.fromhex(client_order_id[:16]), 'big')
```

Take the first 16 hex characters (8 bytes) of the `client_order_id` and
interpret as a big-endian integer.  This is stateless and survives restarts —
retrying the same `client_order_id` reconstructs the identical signed payload.

Implemented in `src/exchanges/polymarket_signing.py::derive_salt`.

### Cancel surface (4 methods)

| Method | Endpoint | Scope |
|---|---|---|
| `cancel(client_order_id)` | `DELETE /order` | Single order by exchange order ID |
| `cancel_all()` | `DELETE /cancel-all` | All open orders for the wallet |
| `cancel_all(market_id)` | `DELETE /cancel-market-orders?market={condition_id}` | All orders in one market |
| _(kill switch cascade)_ | kill switch → `cancel_all(market_id)` | Automatic on kill switch trip |

`cancel()` is idempotent: 404 / already-cancelled → return `False`, not raise.

### Error classification

| Category | Trigger | Retry? |
|---|---|---|
| `retryable` | HTTP 429, 503, 504, network timeout | Yes (with backoff) |
| `auth` | HTTP 401, 403 | No (fix credentials) |
| `not_found` | HTTP 404 | No (order/market gone) |
| `fatal` | Other 4xx, signing error | No |

Error parsing branches on `Content-Type`:
- `application/json`: parse `error` or `message` field
- Otherwise: use raw body text as the reason string

### Rate limit handling

Polymarket rate limits: ~10 req/s per IP on the CLOB.
- On 429: the adapter returns `accepted=False, reason=[retryable]...`
- `BaseBot.place()` does not auto-retry — the strategy's next tick will retry.
- The `tenacity` library is available if auto-retry is needed on specific paths
  (e.g. `cancel_all` on kill switch trip is critical enough to warrant retries).

### CLOB vs Gamma — which API for what

| Need | API | Endpoint |
|---|---|---|
| Tick size, min order size | CLOB | `GET /markets/{condition_id}` |
| Market open/close status | CLOB | `GET /markets/{condition_id}` — use `closed == false AND accepting_orders == true` (NOT `active`) |
| Settlement payout | Gamma | `GET /markets/{condition_id}?closed=true` → `outcomePrices` |
| Order placement | CLOB | `POST /order` |
| Open orders | CLOB | `GET /data/orders` (authenticated) |

**Important:** `active` does NOT flip on market close on Polymarket.  Never use
`active == true` to check tradability.  Use `closed == false AND accepting_orders == true`.

### Gamma JSON-string-encoded arrays

Gamma wraps several fields as JSON-string-encoded arrays (parsed with `json.loads`):
- `clobTokenIds` → `list[str]` of token IDs
- `outcomes` → `list[str]` outcome names
- `outcomePrices` → `list[str]` of Decimal strings (final payouts)
- `umaResolutionStatuses` → `list[str]`

Settlement payout lookup:
```python
clob_token_ids = json.loads(row["clobTokenIds"])
outcome_prices = json.loads(row["outcomePrices"])
idx = clob_token_ids.index(token_id)
payout = Decimal(outcome_prices[idx])
```

Gamma timestamp format: `"2026-03-19 23:20:15+00"` — parse with:
```python
datetime.fromisoformat(s.replace(" ", "T"))
```

### WebSocket subscriptions

**Endpoint:** `wss://ws-subscriptions-clob.polymarket.com/ws/market`

**Subscribe message:**
```json
{"type": "market", "assets_ids": ["<token_id>"]}
```

**Two top-level shapes:**
- `list` — initial book snapshot. Items have no `event_type`. Each item contains
  `asset_id`, `bids`, `asks` arrays. Branch: `isinstance(payload, list)`.
- `dict` — live update. Has `event_type` field. Known types:
  - `price_change` — book update; contains `bids`, `asks`
  - `last_trade_price` / `trade` — fill event
  - `order_cancelled` — cancellation
  - `order_rejected` — rejection

**Reconnect:** exponential backoff starting at 1s, capped at 60s.

### Settlement detection flow

1. Background poll: call `GET {gamma_host}/markets/{condition_id}?closed=true` every 60s.
2. If `closed == true`: parse `outcomePrices` and `clobTokenIds`.
3. Look up `token_id` index; read payout.
4. Emit `SettlementEvent` via the `events()` generator.

### Paper mode

`PolymarketPaperSimulator` (`src/exchanges/polymarket_paper.py`):
- Subscribes to live Polymarket WS for subscribed token IDs.
- Maintains `best_bid`, `best_ask` per token from `price_change` events.
- On `place(intent)`:
  - **Taking order** (BUY price ≥ best_ask or SELL price ≤ best_bid): fill immediately
    at **far touch** (best_ask for BUY, best_bid for SELL).
  - **Resting order**: add to pending book; fill when live book trades through.
- Fee schedule: loaded from Gamma `feeSchedule` at startup (default: 50 bps taker,
  25 bps maker rebate).
- Optional latency injection via `latency_ms` constructor arg.
- Settlement: emits `SettlementEvent` when `SettlementEvent` is observed.

### Wallet safety checks (live mode only)

Enforced in `__aenter__` / startup:

1. **On-chain USDC allowance check** — STUBBED pending Phase 4 wallet integration.
   Requires RPC call to Polygon to read `allowance(wallet, ctf_exchange_contract)`.
   If > `config.max_allowance_usdc`, refuse to start and emit critical alert.

2. **Hot-wallet balance ceiling** — STUBBED. Requires Polygon RPC balance check.
   If balance > `config.balance_ceiling_usdc`, alert at warn.

3. **Signature type verification** — STUBBED. Verify `config.signature_type`
   matches the actual wallet shape (EOA=0, proxy=1, gnosis=2).

These stubs are in `PolymarketAdapter._startup_wallet_checks()`.

### Reconciliation

Called at startup (live mode) and after WS disconnects > threshold.

1. `GET /data/orders?owner={wallet}&status=LIVE` to fetch open orders.
2. Diff against `_inflight` cache.
3. Stale entries (in cache but not on exchange) are removed from cache.
4. Exchange view is adopted as authoritative.
5. If exchange unreachable: `_reconcile_failed = True`.
   - `place()` is refused until reconciliation succeeds.
   - `cancel()` of known orders is still allowed.

---

## Kalshi — Phase TBD

Kalshi uses a REST + FIX interface, not on-chain signing.  Adapter is deferred
until a second strategy requests it.  The `ExchangeAdapter` interface is designed
to accommodate it without changes.

---

## Adding a new exchange

1. Create `src/exchanges/<name>.py`.
2. Subclass `ExchangeAdapter`; set `venue: ClassVar[str] = "<name>"`.
3. Implement all five abstract methods.
4. Update `src/exchanges/echo.py::make_adapter` to handle the new venue name.
5. Write unit tests with fixture-captured API payloads (no live network in tests).
6. Write a paper-mode integration test stub (skipped by default).
7. Update this file with wire-level details for the new exchange.
