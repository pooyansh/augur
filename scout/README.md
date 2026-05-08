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

## Running

With the venv active, `python polymarket_scout.py <subcmd>` works directly.
Without activating, run via the venv's interpreter: `.venv/bin/python polymarket_scout.py <subcmd>`.

## Subcommands

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

## Outputs

Every subcommand writes a jsonl capture to `scout/runs/<utc-ts>-<subcmd>.jsonl`.
Each line is one HTTP request: `{request, response_status, response_headers, response_body, latency_ms}`.

`scout/runs/` is gitignored. Keep captures local for the `learnings/` writeup.
