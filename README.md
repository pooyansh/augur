# PolymarketBidderBot

Automated prediction-market trading bot platform (Polymarket, Kalshi). A central **manager**
spawns and supervises **bots**; each bot runs one strategy against one market. Designed to run
identically on a laptop or a single VPS via `docker compose`.

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

## Stack

- **Python 3.12**, `asyncio`, fully type-hinted, `mypy --strict`
- **uv** for dependency management and lockfile (`uv.lock`)
- **Postgres 16** for durable state (snapshots, orders, trades, audit log)
- **SQLAlchemy 2.x + asyncpg + Alembic** for ORM and migrations
- **Polars** for signal/feature work
- **sops + age** for secrets at rest
- **Pytest + Hypothesis** for tests
- **Pre-commit**: ruff, mypy, sops verification, gitleaks

## Development setup

```bash
uv venv
uv sync --extra dev
uv run pre-commit install
```

Run checks:

```bash
make lint      # ruff + mypy
make test      # pytest
make fmt       # ruff format
```

## Secrets management

All secrets are sops-encrypted YAML in `secrets/`. See `secrets/README.md` for seeding,
editing, and rotation workflows.

```bash
make secrets-init              # generate dev age key (first-time only)
make secrets-edit FILE=infra   # edit and re-encrypt a secrets file
make secrets-rotate FILE=infra # update recipients (e.g. after adding CI key)
```

## Phase 2 status

| Component | Status |
|-----------|--------|
| `pyproject.toml` + `uv.lock` | Scaffolded |
| `src/` package skeleton | Stub `__init__.py` files only |
| `docker/Dockerfile.manager`, `docker/Dockerfile.bot` | Scaffolded (multi-stage, hardened) |
| `docker/entrypoint.sh` | Scaffolded |
| `docker-compose.yml` + `docker-compose.dev.yml` | Scaffolded |
| `secrets/.sops.yaml` | Scaffolded (placeholder recipients) |
| Alembic + first migration (bot_state, audit_log, kill_switch) | Stub tables |
| `.pre-commit-config.yaml` | Configured |
| `.github/workflows/ci.yml` | Configured |
| `.github/workflows/image-scan.yml` | Configured |
| Business logic (strategies, adapters, signals, risk) | Phase 3+ |

## Dashboard

The manager exposes a read-only operator dashboard at `http://127.0.0.1:8090/` by default.

### Accessing the dashboard

**Locally (dev):**

```bash
# The manager binds to 127.0.0.1 only. Open your browser directly:
open http://127.0.0.1:8090/
```

**On a VPS — SSH tunnel:**

```bash
# From your laptop, tunnel port 8090 from the VPS to localhost:
ssh -L 8090:localhost:8090 user@your-vps
# Then open http://127.0.0.1:8090/ in your browser.
```

**On a VPS — Tailscale:**

```bash
# If the VPS is on your Tailscale network, tunnel via ts:
ssh -L 8090:localhost:8090 user@<tailscale-ip>
# Or run tailscale up on the VPS and access via Tailscale IP directly
# once Phase 9 adds the auth story.
```

### CLI flags

```bash
uv run python -m src.manager start \
    --dashboard-port 8090 \      # default
    --dashboard-host 127.0.0.1 \ # must be 127.0.0.1 in v1
    --no-dashboard               # disable the dashboard entirely
```

### Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/health` | Manager + Postgres health |
| `GET /api/status` | Per-bot live heartbeat status |
| `GET /api/bots` | Latest snapshot per bot |
| `GET /api/bots/{id}` | Bot detail + last 50 audit rows |
| `GET /api/strategies` | Per-strategy PnL rollup |
| `GET /api/strategies/{name}` | Per-bot breakdown for one strategy |
| `GET /api/markets` | Per-market exposure |
| `GET /api/audit` | Paginated audit log (filters: bot_id, kind, since, before) |
| `GET /api/failures` | Last-7-day failure timeline |
| `GET /api/capital` | Total + per-exchange balance |

All responses include `ETag` and `Cache-Control` headers; `If-None-Match` returns 304.

The two machine-grade endpoints added in Phase 6a do **not** include ETag headers:

| Endpoint | Description |
|---|---|
| `GET /api/metrics` | Prometheus text format — scrape from Prometheus server via SSH tunnel |
| `GET /api/healthz` | 200 healthy / 503 unhealthy; checks Postgres ping + all bot heartbeat ages |

### Security

- v1 binds to `127.0.0.1` only. Passing `--dashboard-host` with a non-loopback value
  is rejected with an error.
- The `dashboard_reader` Postgres role has `SELECT`-only access. The dashboard cannot
  write to the database even if attacked.
- All free-form text (`last_error`, payloads) is passed through the Phase 3 redaction
  filter before leaving the API.
- Public exposure + auth is a Phase 9 task.

## Observability (Phase 6a)

The platform exposes machine-grade observability on the same dashboard port (8090, loopback-only).

### Prometheus metrics

Scrape `GET /api/metrics` (Prometheus text format) via an SSH tunnel:

```bash
ssh -L 9090:127.0.0.1:8090 user@your-vps
# Add to prometheus.yml:
#   - targets: ["127.0.0.1:9090"]
#     metrics_path: /api/metrics
```

Key metric families: `bot_tick_latency_seconds`, `bot_tick_overrun_total`,
`bot_snapshot_lag_seconds`, `bot_heartbeat_age_seconds`, `order_intent_total`,
`order_fill_latency_seconds`, `signal_fetch_total`, `signal_staleness_seconds`,
`pnl_realized_usd`, `pnl_unrealized_usd`, `position_notional_usd`.

Cardinality rule: `bot_id` and `strategy` are labels; `market_id` is **not**.

### Health check

`GET /api/healthz` returns 200 when Postgres is reachable and all bot heartbeats are within
tolerance. Returns 503 with a diagnostic JSON body listing which checks failed. Suitable for
load-balancer probes or uptime monitors.

### Grafana dashboards

Two Grafana dashboard JSONs are checked into `ops/grafana/`:
- `bot-overview.json` — at-a-glance: tick latency p99, order intent rates, snapshot lag, PnL.
- `per-bot-drilldown.json` — per-bot drill-down with `$bot_id` variable, heatmaps, and signal staleness.

Import via Grafana's "Dashboards → Import → Upload JSON file". See `ops/grafana/README.md`.

### SLOs

Three operational budgets are documented in `ops/SLOs.md`:
1. Tick on-time rate ≥ 99% (7-day rolling).
2. Snapshot lag p99 < 60 s.
3. Order placement success rate ≥ 99% (excluding deliberate `risk_blocked` / `kill_switch`).

Budgets, not contracts — missing them prompts investigation, not a page.

### Log rotation

JSON logs are written to `LOG_DIR` (default `/var/log/manager/`). Configure logrotate with
`ops/logrotate/manager.conf` — daily rotation, 14 days, compressed. See that file for
installation instructions.

## Architecture

See `CLAUDE.md` for the full architecture description, invariants, and subagent/slash-command
reference.
