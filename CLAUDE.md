# CLAUDE.md — Prediction Market Bot Platform

## What this is

A self-hosted platform for running automated betting bots against prediction markets (Polymarket, Kalshi, and additional adapters as needed). A central **manager** spawns and supervises **bots**; each bot runs a single **strategy** against a single **market**, consumes periodic **signals** (e.g. BTC price every 15 min), and places orders via an exchange-specific **adapter**. State is durable, secrets are encrypted at rest, operational events are routed to Slack/Discord/Telegram.

Designed to run identically on a laptop or a single VPS via `docker compose`. No cloud assumptions.

---

## Architecture (one paragraph)

The **manager** is a long-lived supervisor service. It reads `config/bots.yaml` (a roster of desired bot instances), spawns each bot as its own process inside the manager container (v1) or its own container (v2 — interface is identical), and supervises them via heartbeats over a unix socket. Each **bot** is a long-lived async loop that ticks on a fixed schedule, pulls fresh data from a shared **signals layer**, runs its **strategy's** `on_tick`, and places orders through the **exchange adapter** bound to its market. After every tick the bot writes a **state snapshot** to Postgres; on crash the manager rehydrates a fresh process from the latest snapshot (hybrid lifecycle — see below). Secrets live as **sops-encrypted YAML** in `secrets/`, decrypted at entrypoint into a tmpfs mount. Operational events flow through a **severity-routed alerting layer** (Slack / Discord / Telegram).

```
                        ┌──────────────────┐
                        │     Manager      │  supervises, restarts, scales
                        └────────┬─────────┘
                                 │ spawn · heartbeat · snapshot coord
              ┌──────────────────┼───────────────────┐
              ▼                  ▼                   ▼
         ┌──────────┐       ┌──────────┐        ┌──────────┐
         │  Bot A   │       │  Bot B   │        │  Bot C   │
         │ momentum │       │ mean-rev │        │ momentum │
         │ BTC-100K │       │ ELECTION │        │ BTC-150K │
         └────┬─────┘       └────┬─────┘        └────┬─────┘
              │                  │                    │
       ┌──────┴──────┬───────────┴───────────────────┴──┐
       ▼             ▼                                   ▼
   Signals       Exchanges                           State store
   (Binance,    (Polymarket,                         (Postgres)
   Coingecko)    Kalshi, ...)
                                                         │
                                                         ▼
                                                  Alerts router
                                            (Slack · Discord · Telegram)
```

---

## Stack

- **Python 3.12**, `asyncio`, fully type-hinted, `mypy --strict` clean target
- **Polars** for any tabular signal/feature work
- **Postgres 16** for durable state (snapshots, orders, trades, positions, audit log)
- **SQLAlchemy 2.x + asyncpg**, **Alembic** for migrations
- **Docker Compose** for orchestration; single-VPS profile is the default
- **sops + age** for secrets at rest
- **Pytest + Hypothesis** for tests; Hypothesis is mandatory on the order/state machinery
- **Pre-commit**: ruff, mypy, sops verification, secret scanner

Exact pins: `pyproject.toml` and `.claude/rules/00-stack.md`. Do not introduce new heavy dependencies without updating that file.

---

## Bot lifecycle — hybrid (chosen for lowest steady-state latency)

A bot is a long-lived async process. Per-tick cost is **only** strategy compute + exchange round-trip — no container spawn, no full state rehydrate from Postgres. State lives in process memory.

After each successful tick (or on `SIGTERM`), the bot writes a JSONB snapshot to `bot_state(bot_id, snapshot_at, version, state)`. The manager pings each bot's health endpoint every 30s; if a bot misses 2 heartbeats or exits non-zero, the manager spawns a replacement which calls `BaseBot.rehydrate(latest_snapshot)` before entering its loop.

| Property                  | Behavior                                                              |
|---------------------------|-----------------------------------------------------------------------|
| Hot-path latency          | Equal to pure long-lived (no DB read per tick)                        |
| Crash recovery cost       | One tick of replay, max — snapshot frequency is the bound             |
| Rolling deploy            | drain → final snapshot → respawn on new image → rehydrate             |
| Strategy handoff (Bot X → Bot Y on same market) | Y reads X's final snapshot via `market_history` view, then runs its own state |

If a future strategy genuinely needs the pure-respawn shape, the snapshot interface is unchanged — only the supervisor policy flips. Don't build that until a strategy demands it.

Full contract: `.claude/rules/01-bot-contract.md`. State schema, rehydrate semantics, and handoff: `.claude/rules/02-state-handoff.md`.

---

## Bot model — abstract parent + strategy subclasses

Each running bot = `(StrategyClass, market_id, config)`. **One strategy per bot.** Strategies subclass `BaseBot`:

```python
# src/bots/base.py — full contract in .claude/rules/01-bot-contract.md
class BaseBot(ABC):
    name: ClassVar[str]                 # registered strategy name, e.g. "momentum_v1"
    schedule: ClassVar[Schedule]        # cron-like; e.g. every 15 min

    def __init__(self, market: Market, config: BotConfig, deps: BotDeps): ...

    @abstractmethod
    async def on_tick(self, signals: SignalSnapshot) -> Decision: ...

    @abstractmethod
    def snapshot(self) -> dict[str, Any]: ...        # serialized to bot_state.state (JSONB)

    @abstractmethod
    def rehydrate(self, snapshot: dict[str, Any]) -> None: ...

    # Provided by base. DO NOT override:
    async def run(self) -> None: ...                 # main loop, heartbeat, snapshot
    async def place(self, order: OrderIntent) -> OrderResult: ...   # idempotent + risk-checked
```

The manager keeps a registry mapping `strategy_name → BotClass` (`src/manager/registry.py`); strategies are auto-discovered from `src/bots/*/strategy.py` via entry points.

**Adding a new strategy:** `claude /new-strategy momentum_v2` — scaffold defined in `.claude/commands/new-strategy.md`. The slash command generates the strategy module, a paper-mode test, a backtest stub, and a config block.

---

## Exchanges

Each exchange has an adapter implementing `ExchangeAdapter` (`src/exchanges/base.py`). Adapters MUST:

- Accept a deterministic `client_order_id` for idempotency. The base bot generates one per `OrderIntent` and never reuses it.
- Surface settlement, fills, and errors as typed events, not raw API payloads. The strategy never sees an exchange's wire format.
- Be paper-mode aware: `mode=paper` routes through an in-memory simulator that uses live market data but doesn't transmit.

Polymarket and Kalshi are mechanically very different (Polymarket = on-chain CLOB with EIP-712 signed orders; Kalshi = REST + FIX). The adapter interface hides this. Wire-level details live in `.claude/rules/05-exchanges.md`.

---

## Signals

Signals are external time-series feeds (BTC price, election polling, sports lines, on-chain metrics). The signals layer is a shared cache + scheduler so 50 bots watching BTC make 1 Coingecko call, not 50. A bot declares its signal subscriptions; the layer guarantees freshness within the schedule's tolerance and emits `warn` if a feed goes stale.

Cadence is per-source. The 15-min BTC sampler is one feed. Adding a feed: subclass `Signal` in `src/signals/`, register it in `src/signals/registry.py`.

---

## Secrets — sops + age

All secrets are sops-encrypted YAML in `secrets/`, **committed to the repo**:

```
secrets/
├── .sops.yaml           # age recipients (host keys, dev keys, CI key)
├── exchanges.enc.yaml   # API keys, wallet privkeys, per exchange/env
├── alerts.enc.yaml      # Slack, Discord, Telegram webhooks
└── infra.enc.yaml       # DB passwords, internal tokens
```

`docker/entrypoint.sh` decrypts to a **tmpfs mount** before exec'ing the app. Secrets never hit disk in plaintext, never appear in `docker inspect`, never enter env vars unless explicitly mapped by the entrypoint.

The host's age private key is provided out-of-band (e.g. mounted from the VPS host's `~/.config/sops/age/keys.txt` — never copied into the image). For CI, a CI-only age recipient is added to `.sops.yaml` and the key is held in the CI secret store.

Full workflow (rotation, adding recipients, dev vs prod recipient sets): `.claude/rules/03-secrets.md`.

**Hard rule:** Plaintext secrets MUST NEVER be committed, logged, or sent to alert channels. The pre-commit hook runs `sops --verify` on `secrets/*.enc.yaml` and blocks any unencrypted YAML in `secrets/`. The logger has a redaction filter that masks any value matching loaded secret values.

---

## Alerting

Three sinks: Slack, Discord, Telegram. Routing is **severity-based** by default; per-bot overrides allowed.

| Severity   | Default sinks               | Examples                                              |
|------------|-----------------------------|-------------------------------------------------------|
| `info`     | Discord                     | bot started, snapshot written                         |
| `warn`     | Discord + Slack             | signal stale, retryable order error, drawdown 50%     |
| `critical` | Slack + Telegram (paging)   | order rejected, kill switch tripped, bot crash-loop   |

Every alert carries `bot_id`, `strategy`, `market`, `severity`, and a deduplication key (so flapping doesn't spam). Detail: `.claude/rules/04-alerting.md`.

---

## Risk controls — non-negotiable, enforced in `BaseBot.place`

Before any order leaves the bot:

- Per-bot **max position size** (notional), **max daily loss**, **max orders per minute**.
- Global **kill switch** flag in Postgres; checked before every order. Tripping it cancels open orders and freezes all new placement across all bots.
- **Paper mode** is the default for every new strategy until promoted to live by config + allow-list (see invariants).
- Every order intent and result is written to an immutable `audit_log` table.

Tuning guidance and the rationale for each cap: `.claude/rules/06-risk-controls.md`.

---

## Running it

```bash
# one-time
cp .env.example .env                  # non-secret config (db host, log level, ...)
make secrets-init                     # generate dev age key, register in .sops.yaml
make secrets-edit FILE=exchanges      # opens decrypted in $EDITOR, re-encrypts on save

# dev
docker compose -f docker-compose.yml -f docker-compose.dev.yml up

# prod (VPS)
docker compose up -d
docker compose logs -f manager
```

Bot roster lives in `config/bots.yaml` (non-secret — references secret keys by name). Edit and:

```bash
docker compose exec manager python -m manager.reload
```

to apply without bouncing healthy bots.

---

## Testing modes

| Mode      | What it does                                                                       | When mandatory                              |
|-----------|------------------------------------------------------------------------------------|---------------------------------------------|
| Unit      | Pure logic, no I/O. `on_tick` deterministic given (signals, state).                | Always                                      |
| Backtest  | Replay historical signals through `on_tick`. Lives in `tests/strategies/`.         | Before paper                                |
| Paper     | Live signals, simulated fills via adapter's paper mode.                            | Before live, minimum N days (per `.claude/rules/07-testing.md`) |
| Live      | Real money. Requires three locks (see invariants).                                 | Explicit promotion                          |

Detail: `.claude/rules/07-testing.md`.

---

## Project layout

```
.
├── CLAUDE.md                    # this file
├── README.md                    # human-facing
├── docker-compose.yml
├── docker-compose.dev.yml
├── pyproject.toml
├── .env.example
├── config/
│   └── bots.yaml                # roster — strategy, market, mode, secret refs
├── secrets/                     # sops-encrypted; see § Secrets
│   ├── .sops.yaml
│   ├── exchanges.enc.yaml
│   ├── alerts.enc.yaml
│   └── infra.enc.yaml
├── docker/
│   ├── Dockerfile.manager
│   ├── Dockerfile.bot
│   └── entrypoint.sh            # sops decrypt → exec
├── .claude/
│   ├── rules/
│   │   ├── 00-stack.md
│   │   ├── 01-bot-contract.md
│   │   ├── 02-state-handoff.md
│   │   ├── 03-secrets.md
│   │   ├── 04-alerting.md
│   │   ├── 05-exchanges.md
│   │   ├── 06-risk-controls.md
│   │   └── 07-testing.md
│   ├── commands/
│   │   ├── new-strategy.md      # slash: scaffold a strategy
│   │   ├── new-exchange.md      # slash: scaffold an adapter
│   │   ├── inspect-state.md     # slash: pretty-print a bot's latest snapshot
│   │   └── deploy-bot.md        # slash: paper → live promotion checklist
│   └── agents/
│       ├── bot-developer.md          # subagent for strategy work
│       └── exchange-adapter-developer.md  # subagent for adapters; higher review bar
├── src/
│   ├── manager/                 # supervisor, registry, lifecycle
│   ├── bots/
│   │   ├── base.py              # BaseBot
│   │   ├── momentum/
│   │   └── mean_reversion/
│   ├── exchanges/
│   │   ├── base.py              # ExchangeAdapter ABC
│   │   ├── polymarket.py
│   │   └── kalshi.py
│   ├── signals/
│   ├── alerts/
│   ├── state/                   # SQLAlchemy models, snapshot I/O, migrations
│   ├── secrets/                 # loader, redaction filter
│   └── risk/                    # caps, kill switch, audit
└── tests/
    ├── unit/
    ├── integration/
    └── strategies/              # backtests
```

---

## Subagents and slash commands

Use the registered subagents for scoped work — they have narrower tool budgets and stricter rules:

- **`bot-developer`** — writing or modifying a strategy. Knows `BaseBot`, signals, paper-mode test patterns. Cannot touch `src/exchanges/`, `src/risk/`, or `secrets/`.
- **`exchange-adapter-developer`** — new exchanges or adapter changes. Higher review bar; must produce both unit tests against recorded fixtures and a paper-mode integration test.

Slash commands (definitions in `.claude/commands/`):

| Command | Purpose |
|---|---|
| `/new-strategy <name>` | Scaffold a new strategy module + tests + config block |
| `/new-exchange <name>` | Scaffold an adapter with paper-mode + fixture-driven tests |
| `/inspect-state <bot_id>` | Pretty-print a bot's latest snapshot for debugging |
| `/deploy-bot <bot_id>` | Run the paper-to-live promotion checklist |

---

## Where to look first

| If you're… | Read |
|---|---|
| Adding a strategy | `.claude/rules/01-bot-contract.md` then `/new-strategy` |
| Adding an exchange | `.claude/rules/05-exchanges.md` then `/new-exchange` |
| Tuning risk | `.claude/rules/06-risk-controls.md` |
| Debugging a crashed bot | `/inspect-state <bot_id>` |
| Rotating a secret | `.claude/rules/03-secrets.md` |
| Deploying to a VPS | `README.md` § Deploy |
| Promoting paper → live | `/deploy-bot` |

---

## Critical invariants — never violate

1. **Three locks for live trading.** `--mode live` flag AND per-bot `live: true` config AND the bot id in the manager-level live allow-list. Missing any one ⇒ paper mode.
2. **Every order carries a deterministic `client_order_id`.** Idempotency is the only thing standing between us and double-spending on retries. Generated by `BaseBot`, never by strategies.
3. **Risk checks live in `BaseBot.place`, not in strategies.** A buggy strategy cannot bypass the caps or the kill switch.
4. **Plaintext secrets never touch disk outside tmpfs, never enter env vars without explicit mapping, never appear in logs or alerts.** The logger redaction filter is mandatory.
5. **`audit_log` is append-only.** No deletes, no updates. Corrections are new rows that reference the original.
6. **Snapshots are best-effort but must not block the tick.** Snapshot failures alert at `warn`; the tick continues. The manager handles the worst case via rehydrate.
7. **All time is UTC, NTP-synced.** Markets settle on wall-clock boundaries; clock skew is a correctness issue.
