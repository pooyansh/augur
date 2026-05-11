# /inspect-state \<bot_id\>

Pretty-print the latest snapshot for a running (or previously-run) bot.

## Usage

```
/inspect-state <bot_id>
```

## What it does

1. Connects to Postgres using the `POSTGRES_*` environment variables
   (`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`,
   `POSTGRES_PASSWORD`).
2. Fetches the most recent row from `bot_state` for the given `bot_id`.
3. Prints a JSON object with:
   - `bot_id` — the stable bot identifier
   - `market_id` — the market this bot trades
   - `snapshot_at` — UTC ISO-8601 timestamp of the snapshot
   - `version` — monotonic snapshot version counter
   - `state` — the full JSONB strategy state (pretty-printed)
4. If `bot_id` is not found in the table, lists all known bot ids from
   `bot_state` so you can pick the right one.

## CLI invocation

```bash
uv run python -m src.manager inspect-state <bot_id>
```

Or via the manager CLI group:

```bash
uv run python -m src.manager inspect-state echo-paper-1
```

## Implementation

`src/manager/inspect_state.py` — `run_inspect(bot_id)` async function,
importable and called by the `inspect-state` subcommand in
`src/manager/__main__.py`.

## Example output

```json
{
  "bot_id": "echo-paper-1",
  "market_id": "ECHO-TEST",
  "snapshot_at": "2026-05-09T12:34:56.789000+00:00",
  "version": 42,
  "state": {
    "_version": 42,
    "intent_seq": 7,
    "last_decision_at": "2026-05-09T12:34:56.123456+00:00",
    "position": "0",
    "tick_count": 7
  }
}
```

## Prerequisites

- `POSTGRES_HOST` must be set in the shell environment.
- Postgres must be reachable and the `bot_state` table must exist
  (run Alembic migrations first: `uv run alembic upgrade head`).
