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

### Security

- v1 binds to `127.0.0.1` only. Passing `--dashboard-host` with a non-loopback value
  is rejected with an error.
- The `dashboard_reader` Postgres role has `SELECT`-only access. The dashboard cannot
  write to the database even if attacked.
- All free-form text (`last_error`, payloads) is passed through the Phase 3 redaction
  filter before leaving the API.
- Public exposure + auth is a Phase 9 task.

## Architecture

See `CLAUDE.md` for the full architecture description, invariants, and subagent/slash-command
reference.
