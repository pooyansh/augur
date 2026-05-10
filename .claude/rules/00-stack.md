---
phase_added: 2
last_reviewed: 2026-05-09
---

# 00 — Stack and dependency rules

## Runtime

| Concern | Library | Version pin | Notes |
|---|---|---|---|
| Python | CPython | `>=3.12,<3.13` | 3.12 only; 3.13 not yet tested |
| Async framework | stdlib `asyncio` | (stdlib) | No third-party event loop |
| Tabular / signal work | `polars` | `>=1.0` | Never `pandas` |
| HTTP client | `httpx` | `>=0.27` | Keep one long-lived `AsyncClient` per adapter |
| WebSocket | `websockets` | `>=12` | |
| YAML | `pyyaml` | `>=6` | Safe-load only; never `yaml.load` without Loader |
| Validation models | `pydantic` | `>=2.7` | V2 API only |
| Structured logging | `structlog` | `>=24` | |
| Retry logic | `tenacity` | `>=8` | |
| CLI | `click` | `>=8` | |
| DB ORM | `sqlalchemy[asyncio]` | `>=2.0` | Async sessions only (`asyncpg` driver) |
| DB driver | `asyncpg` | `>=0.29` | |
| Migrations | `alembic` | `>=1.13` | Write migrations manually; never autogenerate against a live DB in CI |

## Dev / test

| Concern | Library | Version pin |
|---|---|---|
| Test runner | `pytest` | `>=8` |
| Async tests | `pytest-asyncio` | `>=0.23` |
| Property testing | `hypothesis` | `>=6` |
| Type checker | `mypy` | `>=1.10` — `--strict` mode |
| Linter / formatter | `ruff` | `>=0.5` |
| Dependency audit | `pip-audit` | `>=2.7` |
| Pre-commit hooks | `pre-commit` | `>=3.7` |

## Rules

1. **No new heavy dependency without updating this file AND an ADR** in
   `docs/adr/` if it introduces a new category (e.g. a message broker, a
   new crypto library).
2. `uv` is the only package manager.  Never use `pip install` directly.
   Add deps with `uv add <pkg>`; update lockfile with `uv lock`.
3. `mypy --strict` must pass on `src/`.  New code that can't satisfy strict
   mode requires an explicit waiver comment (`# type: ignore[reason]`) and
   a follow-up issue.
4. `ruff check` and `ruff format --check` must pass on every commit (enforced
   by pre-commit).
5. `pip-audit` runs in CI; any known CVE blocks the build.
6. `polars` for tabular work; `decimal.Decimal` for all money, prices, and
   sizes — **never** `float`.
