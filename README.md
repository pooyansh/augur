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

## Architecture

See `CLAUDE.md` for the full architecture description, invariants, and subagent/slash-command
reference.
