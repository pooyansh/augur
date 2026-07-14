# Architecture — High-Level Diagram

A human-readable, big-picture view of the platform. For the detailed internal-flow diagram, see [`architecture-diagram-detailed.md`](architecture-diagram-detailed.md). For implementation specifics, see [`architecture-summary.md`](architecture-summary.md). This diagram supersedes the ASCII sketch in `architecture.md` with the pieces added since (`ml/`, `src/rules/`, the dashboard control route).

```mermaid
flowchart TB
    operator["Operator<br/>(you)"]

    subgraph manager_box["Manager (long-lived supervisor process)"]
        supervisor["Supervisor<br/>spawn · heartbeat · reload · stop"]
        dashboard["Dashboard<br/>read API (GET) + control API (1 route)"]
    end

    subgraph bots_box["Bots (one process per bot)"]
        botA["Bot: momentum_v1<br/>market: BTC-UPDOWN-5m"]
        botB["Bot: momentum_v1<br/>market: ..."]
    end

    signals["Signals layer<br/>shared cache + scheduler<br/>(Coingecko, Binance, ...)"]
    exchanges["Exchange adapters<br/>Polymarket CLOB · paper simulator"]
    risk["Risk & rules<br/>caps · kill switch · winning rules"]
    state[("Postgres<br/>bot_state · audit_log · perf_rollup")]
    alerts["Alerts<br/>Slack · Discord · Telegram"]

    subgraph ml_box["ml/ — offline, sibling package (never imported by src/)"]
        collectors["Historical data collectors<br/>Tier 1 events · Tier 2 per-second trades"]
        parquet[("Parquet datasets<br/>partitioned by venue/series")]
    end

    operator -->|"config/bots.yaml"| supervisor
    operator -->|"browser, via SSH tunnel on a VM"| dashboard
    supervisor -->|spawns & monitors| bots_box
    dashboard -->|reads| state
    dashboard -.->|"stop_bot() — in-process, no DB write"| supervisor

    botA <--> signals
    botB <--> signals
    botA --> exchanges
    botB --> exchanges
    botA --> risk
    botB --> risk
    botA -->|snapshot + audit| state
    botB -->|snapshot + audit| state
    risk -.->|critical events| alerts
    supervisor -.->|bot lifecycle events| alerts

    collectors -->|"Polymarket's own public API<br/>(Gamma, data-api — no auth)"| exchanges_data["Polymarket<br/>(external)"]
    collectors --> parquet
    parquet -.->|future: training| ml_future["ml/features, ml/datasets,<br/>model training (not built yet)"]

    exchanges -->|orders, fills, settlement| exchange_ext["Polymarket<br/>(external, live market)"]

    classDef offline fill:#f5f5f5,stroke:#999,stroke-dasharray: 5 5,color:#333
    class ml_box,collectors,parquet,ml_future offline
```

## Reading this diagram

| Box | What it is | Key fact |
|---|---|---|
| **Manager** | One long-lived process | Runs the `Supervisor` (spawns/monitors bots) and the `Dashboard` (read-only web UI + the one control route) in the same process |
| **Bots** | One OS subprocess per configured bot | Each runs a single strategy against a single market, on its own tick schedule |
| **Signals layer** | Shared fetch/cache scheduler | 50 bots watching BTC price make 1 upstream call, not 50 |
| **Exchange adapters** | Translate strategy intent ↔ exchange wire format | Polymarket CLOB (live) or an in-memory paper simulator — same interface either way |
| **Risk & rules** | Caps, kill switch, and the new optional provisional winning-rule framework | Enforced inside `BaseBot.place()` — a strategy cannot bypass it |
| **Postgres** | Durable state | `bot_state` (snapshots), `audit_log` (append-only), `perf_rollup` (materialized view for the dashboard) |
| **Dashboard** | Read-only web UI, one narrow exception | Everything is `GET` except one `POST /api/control/bots/{id}/stop`, which never touches Postgres |
| **`ml/`** | Offline, disconnected from live trading | A sibling package that collects Polymarket's own historical market data for future model training — not wired into `src/` at all |

The dashed box is offline/optional infrastructure — it doesn't run on the trading path and nothing in it can affect a live bot.
