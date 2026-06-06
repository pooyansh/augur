# System Architecture — Prediction Market Bot Platform

## Overview

A self-hosted platform for running automated trading bots against prediction markets (Polymarket today; Kalshi adapter deferred). A central **manager** spawns and supervises **bots**; each bot runs one **strategy** against one **market**, reads periodic **signals** (e.g. BTC price every 15 min), and places orders via an exchange-specific **adapter**.

The platform is designed to run identically on a laptop or a single VPS via `docker compose`. No cloud infrastructure is assumed. New markets and strategies are added by editing `config/bots.yaml` and (when new strategy logic is needed) adding a Python module under `src/bots/`.

---

## Component Map

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              Manager                                    │
│  Reads config/bots.yaml · Spawns subprocesses · Watchdog loop          │
│  Dashboard (FastAPI, port 8090, loopback-only)                          │
└────────┬──────────────┬─────────────────────┬───────────────────────────┘
         │              │                     │
         ▼              ▼                     ▼
   ┌──────────┐   ┌──────────┐          ┌──────────┐
   │  Bot A   │   │  Bot B   │    ...   │  Bot N   │
   │ momentum │   │ momentum │          │ <any>    │
   │ btc-5m   │   │ btc-15m  │          │          │
   └────┬─────┘   └────┬─────┘          └────┬─────┘
        │              │                      │
   ┌────┴──────────────┴──────────────────────┴────┐
   │               Signals Runtime                  │
   │   Shared cache · One fetch per cadence         │
   │   btc_15min (Coingecko + Binance fallback)     │
   └───────────────────┬───────────────────────────┘
                       │
   ┌───────────────────┴───────────────────────────┐
   │           Exchange Adapters                    │
   │  Polymarket CLOB (EIP-712 + HMAC)             │
   │  PolymarketPaperSimulator (paper mode)         │
   │  EchoAdapter (unit tests)                      │
   └───────────────────┬───────────────────────────┘
                       │
   ┌───────────────────┴───────────────────────────┐
   │               Postgres 16                      │
   │  bot_state · audit_log · kill_switch           │
   │  signal_samples · perf_rollup                  │
   └───────────────────────────────────────────────┘
                       │
   ┌───────────────────┴───────────────────────────┐
   │               Alerts Router                    │
   │   Discord (info) · Slack (warn+)               │
   │   Telegram (critical paging)                   │
   └───────────────────────────────────────────────┘
```

---

## Config-Driven Bot Model

Every running bot is fully described by one entry in `config/bots.yaml`. No code change is needed to run the same strategy against a different market with different conditions.

```yaml
# Each entry is one bot instance
- id: btc-updown-5m-1          # stable; used as DB key and audit seed
  strategy: momentum_v1         # which strategy class to instantiate
  market:
    exchange: polymarket
    slug: "btc-updown-5m"       # dynamic: resolver finds the active window
    outcome: "UP"               # which outcome token to trade
  mode: paper                   # paper | live (three-lock rule for live)
  schedule: "every:300s"        # tick interval (overrides strategy ClassVar)
  signals:
    - name: btc_15min           # signals this bot subscribes to
      params: {}
  params:                       # per-bot strategy trigger conditions
    momentum_threshold_pct: "0.3"
    target_size: "5"
    max_ticks_in_position: 3
    buy_price: "0.35"
    sell_price: "0.25"
  risk:
    max_position_notional: "50"
    max_daily_loss: "10"
    max_orders_per_minute: 5
  secrets:
    exchange_credentials: "polymarket.live"  # symbolic key into secrets/*.enc.yaml
```

**Key design point:** `params` carries all market-specific trigger conditions. The strategy reads these at `__init__` with ClassVar defaults as fallback. Two bots on different markets use the same `momentum_v1` class but different `params` blocks — no Python subclass needed per market.

---

## Tick Data Flow

```
1. SignalsRuntime.snapshot_for(bot)
   └─ returns cached SignalSnapshot (or fetches if stale)
         samples: {"btc_15min": {"price_usd": "105000.00"}}
         stale:   set()   ← non-empty if all sources failed + cache expired

2. BaseBot.run() → Heartbeat.beat()

3. KillSwitchReader.is_tripped()?
   └─ yes: adapter.cancel_all(market_id); skip on_tick; sleep

4. strategy.on_tick(signals) → Decision
   └─ returns: intents=[OrderTemplate(...)]  cancels=[]

5. for tmpl in decision.intents:
   └─ BaseBot.place(tmpl):
         a. Assign client_order_id (blake2s, deterministic)
         b. Check kill switch
         c. Check _inflight dedup cache (idempotency)
         d. Check risk caps (notional, daily loss, orders/min)
         e. audit_log.write(kind="order_submitted")
         f. adapter.place(intent) → OrderResult
         g. audit_log.write(kind="order_accepted" | "order_rejected")
         h. Cache result in _inflight

6. BaseBot._persist_snapshot()   ← best-effort; warn on failure, tick continues

7. sleep until next scheduled tick boundary
```

---

## Manager / Supervisor

**Entry point:** `python -m src.manager start --bots-file config/bots.yaml`

1. **Roster load** — validates the full `bots.yaml` via Pydantic. One invalid entry aborts before spawning anything.
2. **Subprocess spawn** — each bot runs as `uv run python -m src.bots.runner --bot-id <id>`. The runner resolves market IDs, loads secrets, builds the adapter, and enters the bot's async loop.
3. **Watchdog** — polls each subprocess every 5 s. If a bot exits non-zero or misses 2 heartbeats, it is respawned. The replacement calls `BaseBot.rehydrate(latest_snapshot)` before its first tick — at most one tick of replay loss.
4. **Heartbeat** — v1: `LocalHeartbeat` (in-process). Phase 5: unix-socket heartbeat the manager monitors externally.
5. **Signal runtime** — started once in the manager; shared across all bots. 50 bots watching `btc_15min` → 1 upstream fetch per cadence.
6. **Rolling deploy** — `SIGTERM` drains: each bot writes a final snapshot, exits cleanly. Manager respawns on new image; bots rehydrate.
7. **Live allow-list** — an additional file (`/run/secrets/live_allowlist.yaml`) checked at spawn time. A bot id absent from the list is downgraded to paper even if `mode: live` in config (third of the three locks).

---

## Bot Model

Every strategy subclasses `BaseBot` and implements exactly three methods:

```python
class MyStrategy(BaseBot):
    name: ClassVar[str] = "my_strategy"         # registry key
    schedule: ClassVar[Schedule] = Schedule(every_seconds=900)  # default; overridden by config

    async def on_tick(self, signals: SignalSnapshot) -> Decision: ...
    def snapshot(self) -> dict[str, Any]: ...    # serialised to bot_state.state (JSONB)
    def rehydrate(self, snapshot: dict[str, Any]) -> None: ...
```

Strategies **must not**:
- Override `run`, `place`, or any risk/audit method.
- Call `self._deps.adapter.place()` directly.
- Generate `client_order_id` values.

**`client_order_id` idempotency** — generated by `BaseBot` as `blake2s(f"{bot_id}:{intent_seq}".encode(), digest_size=8).hexdigest()`. `intent_seq` is a monotonic counter persisted in every snapshot. Retrying the same logical order reconstructs the identical ID, so the adapter and exchange can detect and deduplicate retries.

**Paper vs live** — the distinction is entirely in the adapter layer. `BaseBot` code is identical. `Mode.PAPER` routes through `PolymarketPaperSimulator` (live WS book, simulated fills); `Mode.LIVE` routes through the real CLOB.

---

## Signals Platform

A **Signal** declares what to fetch, how often, and from which sources (in priority order). The runner manages scheduling, caching, and staleness.

```python
class Btc15Min(Signal):
    name = "btc_15min"
    cadence_seconds = 900       # fetch every 15 min
    tolerance_seconds = 1800    # stale if no fresh data within 30 min
    sources = [BtcCoingeckoSource, BtcBinanceSource]  # fallback order

    def parse(self, source_name, raw) -> dict:
        return {"price_usd": str(raw["price"])}   # canonical shape
```

**Invariants:**
- `parse()` outputs canonical shapes only — no derived features (rolling averages, z-scores belong in strategy code).
- Multi-source fallback order is code, not config — visible and testable.
- 50 bots subscribing to `btc_15min` → 1 fetch loop, 1 upstream call per cadence.
- Staleness is data, not an exception. `SignalSnapshot.stale` is a set of signal names; strategies read it and decide (skip, reduce position, etc.).
- `signal_samples` table is append-only — no updates, no deletes.

**Backtests** inject `SignalReplay` (same `SignalsProtocol` interface) — strategy code is unchanged.

---

## Exchange Adapters

All adapters implement `ExchangeAdapter` from `src/exchanges/base.py`:

```python
class ExchangeAdapter(ABC):
    venue: ClassVar[str]
    async def place(self, intent: OrderIntent) -> OrderResult: ...
    async def cancel(self, client_order_id: str) -> bool: ...
    async def cancel_all(self, market_id: str | None = None) -> int: ...
    async def get_market(self, market_id: str) -> Market: ...
    def events(self) -> AsyncIterator[ExchangeEvent]: ...
```

Strategies never see wire formats. The adapter translates to/from typed structures.

### Polymarket adapter

**Authentication:** Two tiers.
- **L1** — EIP-712 typed-data signed with wallet private key (on-chain order validity).
- **L2** — HMAC-SHA256 headers for REST API auth (`POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`, `POLY_API_KEY`, `POLY_PASSPHRASE`).

**Market data:** CLOB only (`clob.polymarket.com`). Gamma API is used solely for slug → condition_id / token_id metadata resolution at startup. **Never use Gamma for prices or settlement** — Gamma data is delayed.

**Settlement detection:** Poll `GET /markets/{condition_id}` on the CLOB every 30 s after market close. When `closed == true`, read `tokens[].winner` and `tokens[].price` (1=win, 0=loss). No Gamma needed.

**Paper simulator** (`PolymarketPaperSimulator`): subscribes to live CLOB WebSocket, maintains best bid/ask per token, fills orders against the live book — no real money.

**Wallet safety checks** (live mode, at startup):
1. Signature type consistency check.
2. USDC allowance cap via on-chain `eth_call` (Polygon RPC, pure httpx).
3. USDC balance ceiling check (warn only, doesn't block startup).
RPC failures are best-effort — logged at warning, startup continues.

---

## Secrets & Security

All secrets are sops-encrypted YAML in `secrets/`, committed to the repo.

```
secrets/
├── .sops.yaml           # age recipients (dev / CI / prod)
├── exchanges.enc.yaml   # API keys, wallet private keys
├── alerts.enc.yaml      # Slack, Discord, Telegram webhooks
└── infra.enc.yaml       # DB passwords, internal tokens
```

`docker/entrypoint.sh` decrypts to a **tmpfs mount** (`/run/secrets/`) at container start. Secrets never hit disk, never appear in `docker inspect`, never enter env vars except via explicit mapping.

**Three-lock rule for live trading** (all three must be true):
1. `mode: live` in `config/bots.yaml`.
2. `live: true` in `BotConfig` (set by runner when mode is live).
3. Bot id present in `/run/secrets/live_allowlist.yaml`.

Missing any one → paper mode.

**RedactionFilter** — installed on the root logger and alert router at startup. Masks any loaded secret value in logs and outbound alert messages. Short values (< 8 chars) are excluded to avoid false positives.

---

## Risk Controls

All risk enforcement lives in `BaseBot.place()` — strategies cannot bypass it.

| Control | Mechanism | On breach |
|---|---|---|
| `max_position_notional` | Per-bot open position cap | `RiskCapExceeded` raised; no order sent |
| `max_daily_loss` | Cumulative P&L loss today (UTC reset) | `RiskCapExceeded` raised |
| `max_orders_per_minute` | Rolling 60-second window | `RiskCapExceeded` raised |
| Kill switch | Postgres `kill_switch` table, ~1s cache | `KillSwitchTripped` raised; cascade `cancel_all` on first trip |

**`audit_log` is append-only** — no UPDATE, no DELETE. The DB role used by the application has no UPDATE/DELETE privileges on `audit_log`. Corrections are new rows referencing the original via `payload["references_audit_id"]`.

---

## Alerting

Three sinks with severity-based routing:

| Severity | Sinks | Examples |
|---|---|---|
| `info` | Discord | Bot started, snapshot written |
| `warn` | Discord + Slack | Signal stale, retryable error, drawdown 50% |
| `critical` | Slack + Telegram (pages) | Order rejected, kill switch tripped, crash-loop |

Every alert carries `bot_id`, `strategy`, `market`, `severity`, and a deduplication key (`blake2s(f"{bot_id}|{severity}|{template_key}".encode())`). Identical alerts are suppressed for 5 minutes.

Sink failures are isolated — one sink failing never blocks other sinks. Failures are logged at warning, never raised.

---

## Observability

**Structured logging** — `structlog`. Every log line includes:
- `ts` — ISO 8601 UTC timestamp
- `level` — log level string
- `event` — human-readable description
- `bot_id` — present in bot context
- `tick_id` — present inside a tick (correlation ID, propagated via ContextVar)

`tick_id` = `blake2s(f"{bot_id}:{intent_seq}:{tick_start_iso}".encode(), digest_size=8).hexdigest()` — lets you correlate all log lines, audit entries, and alert payloads from a single tick.

**Prometheus metrics** — `GET /api/metrics` on the dashboard FastAPI app (port 8090, `127.0.0.1` only). Prometheus scrapes via SSH tunnel in v1. Labels: `bot_id`, `strategy` — `market_id` is NOT a label (unbounded cardinality).

**Health endpoint** — `GET /api/healthz`. Returns 200 when Postgres is reachable and all bot heartbeats are within tolerance. Returns 503 with the same JSON shape on failure.

---

## Dashboard

`GET`-only FastAPI app served at `127.0.0.1:8090` (loopback-only in v1; bearer-token/OIDC auth is a Phase 9 decision).

**Two asyncpg pools:**
- Write pool — uses `POSTGRES_USER`. Used by supervisor and all repositories.
- Read-only pool — uses `dashboard_reader` role (SELECT only; INSERT/UPDATE/DELETE raise at the driver level). All dashboard API handlers use this pool.

**Materialized view `perf_rollup`** — refreshed every 60 s by a background task (`REFRESH MATERIALIZED VIEW CONCURRENTLY`). All aggregated data endpoints (`/api/strategies`, `/api/markets`) query this view — no ad-hoc GROUP BY against raw tables.

**ETag + Cache-Control** on every GET — prevents stale browser caches without CDN complexity.

**Static bundle** — compiled to `web/dist/` and COPY-ed into the manager Docker image. No CDN fetch at runtime; all assets served from the same origin.

---

## Directory Layout (key paths)

```
src/
├── manager/          # supervisor, registry, config schema, CLI
├── bots/
│   ├── base.py       # BaseBot, BotConfig, BotDeps, place(), snapshot
│   ├── runner.py     # subprocess entry point for each bot
│   └── momentum/     # momentum_v1 strategy
├── exchanges/
│   ├── base.py       # ExchangeAdapter ABC, typed structures
│   ├── polymarket.py # CLOB adapter + paper simulator
│   └── echo.py       # in-memory adapter for tests
├── signals/          # Signal, SignalSource, SignalsRuntime, SignalReplay
├── alerts/           # AlertRouter, severity routing, dedup, sinks
├── risk/             # caps.py, kill_switch.py, audit.py, withdrawal_allowlist.py
├── state/            # SQLAlchemy models, StateRepository, migrations (Alembic)
├── secrets/          # loader, RedactionFilter
└── observability/    # metrics registry, tick_id ContextVar, Prometheus handler

config/
└── bots.yaml         # bot roster (non-secret; references secret keys by name)

secrets/              # sops-encrypted; committed to repo
tests/
├── unit/             # no DB, no network; Hypothesis on risk/order machinery
├── integration/      # real Postgres via PG_DSN env var; skipped if unset
├── strategies/       # backtests via SignalReplay
└── fixtures/         # EchoExchange, NullStrategy, ManualClock, InMemory* fakes
```
