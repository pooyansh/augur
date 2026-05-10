# Polymarket Scout

Phase 1 throwaway CLI for probing the Polymarket public API. No auth required for read-only
subcommands. Raw captures land in `scout/runs/` (gitignored).

## Setup (uv)

```bash
cd scout
uv venv                       # creates .venv with the system Python 3.12
source .venv/bin/activate      # so uv targets the local venv, not conda
uv pip install -r requirements.txt
```

If you don't want to activate, set `VIRTUAL_ENV` for the install step:

```bash
VIRTUAL_ENV="$PWD/.venv" uv pip install -r requirements.txt
```

## Auth/signing setup

The signing subcommands (`init-wallet`, `wallet-info`, `derive-api-keys`,
`place-resting-order`, `cancel-order`, `replay-same-nonce`, `bad-orders`) require
additional packages. Install into the same venv:

```bash
source .venv/bin/activate
uv pip install -r requirements-signing.txt
```

Or without activating:

```bash
VIRTUAL_ENV="$PWD/.venv" uv pip install -r requirements-signing.txt
```

### Key file

The L1 EOA private key is read from a file whose path is controlled by:

```
POLYMARKET_SCOUT_KEYFILE   (default: $HOME/.polymarket-scout/disposable.key)
```

The file must have mode **0600**. If it doesn't, every signing subcommand exits immediately
with a clear error. Generate a fresh key with:

```bash
python polymarket_scout.py init-wallet
```

> **DISPOSABLE WALLET.** Hard cap: deposit no more than $10 of MATIC/USDC on mainnet.
> Never reuse this key for any production bot. Never commit it.

Derived L2 API credentials are stored alongside the key file:
`$HOME/.polymarket-scout/disposable.apikeys.json` (also mode 0600).

## Running

With the venv active, `python polymarket_scout.py <subcmd>` works directly.
Without activating, run via the venv's interpreter: `.venv/bin/python polymarket_scout.py <subcmd>`.

## Subcommands

### Read-only (no auth required)

```
list-markets              List active markets from Gamma API
                          --limit N (default 20), --offset N (default 0), --closed flag

get-market <condition_id> Fetch market from both CLOB and Gamma APIs; print schema diff

get-orderbook <token_id>  Fetch top-5 orderbook levels + spread/mid + latency

get-trades <condition_id> Fetch recent trades (may require auth — captures the error)
                          --limit N (default 20)

watch-stream <token_id>   Subscribe to WS book updates; log to runs/ jsonl
  [<token_id>...]         --duration N seconds (default 60, max 600)

find-resolved             List recently resolved markets with outcome prices
                          --limit N (default 20)

probe-errors              Fire malformed requests; capture error shapes to runs/

probe-rate-limit          Burst requests until 429 or exhausted
                          --requests N (default 200), --concurrency N (default 20)
```

### Auth/signing subcommands (require signing extras)

```
init-wallet               Generate a fresh disposable EOA; write hex private key to keyfile.
                          Prints address + safety banner.  --force to overwrite existing.

wallet-info               Print wallet address from keyfile.
                          --rpc <url>   fetch on-chain MATIC + USDC.e balance via Polygon RPC
                          --usdc 0x...  override USDC contract address (for testnet)

derive-api-keys           Derive (or create) L2 CLOB API credentials from the EOA.
                          Stores keys at <keyfile-stem>.apikeys.json (mode 0600).
                          Prints only the first 8 chars of api_key + wallet address.
                          --host <url>       CLOB host (default: https://clob.polymarket.com)
                          --chain-id N       137=Polygon mainnet, 80002=Amoy (default: 137)

place-resting-order       Place a limit order well off the touch.
                          --token-id <id>    ERC-1155 token id (required)
                          --side BUY|SELL    order side (required)
                          --price <float>    limit price; BUY<=0.20, SELL>=0.80 by default
                          --size <float>     size in outcome tokens; price*size <= 5 USDC (hard cap)
                          --allow-near-touch skip the safety price guard (warns loudly)
                          --host <url>       CLOB host
                          --chain-id N       chain ID
                          Captures pre-sign EIP-712 struct + CLOB response to runs/ jsonl.

cancel-order <order_id>   Cancel by order ID; repeats cancel a second time.
                          Second response reveals cancel-idempotency behaviour.
                          --host <url>, --chain-id N

replay-same-nonce <order_id>
                          Fetch original order params, rebuild with the same salt, submit twice.
                          Observes whether Polymarket deduplicates on salt+signature.
                          --salt <int>       explicit salt override (if CLOB no longer has the order)
                          --host <url>, --chain-id N

bad-orders                Fire five deliberately invalid signed orders; capture error shapes.
                          Probes: wrong tick, size below minimum, zero allowance, expired,
                          wrong signature type.
                          --token-id <id>    token to use for probes (required)
                          --host <url>, --chain-id N
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `POLYMARKET_SCOUT_KEYFILE` | `$HOME/.polymarket-scout/disposable.key` | Path to hex private key file (must be mode 0600) |

## Outputs

Every subcommand writes a jsonl capture to `scout/runs/<utc-ts>-<subcmd>.jsonl`.
Each line is one HTTP request: `{request, response_status, response_headers, response_body, latency_ms}`.

Sensitive fields (`signature`, `private_key`, `api_secret`, `passphrase`) are
**redacted to `***REDACTED***`** in all capture files and log lines.
The EIP-712 order struct fields (salt, maker, tokenId, amounts, etc.) are captured in
full before signing — only the `signature` field itself is redacted.

`scout/runs/` is gitignored. Keep captures local for the `learnings/` writeup.
