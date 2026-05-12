# Phase 6b — Operator dashboard

**Goal:** A single URL the operator opens to see what the platform is doing —
which bots are alive, which strategies are winning, how much capital is deployed,
and what just failed. Alerts (Phase 6) page on failure; this is the
"nothing is alerting — am I making money?" surface.

Lands before Phase 7's paper run alongside (or in place of) Phase 6a.

## Why this is a separate phase from 6a

Phase 6a's path is Prometheus + Grafana — operator-grade ops dashboards rendered
server-side. This phase is the human-facing dashboard, with one hard requirement
Grafana cannot satisfy:

- **Compute belongs on the client.** When the operator opens the dashboard from a
  phone or laptop, the rendering, filtering, and charting run on that device. The
  manager process supervises bots; it cannot afford to render charts. Grafana
  renders server-side and so isn't a fit.

6a stays in the plan as the optional machine-grade metrics path; 6b is what
gets opened by a human. They're complementary, not exclusive.

## Architecture (one paragraph)

A FastAPI router mounted inside the manager's asyncio loop exposes a small set of
read-only JSON endpoints on a `127.0.0.1`-bound port. A static SPA (Vite + React +
TypeScript, built to a single bundle and served from the manager image) handles
all rendering, sorting, filtering, and charting in the browser. Expensive
aggregations live in a Postgres materialized view (`perf_rollup`) that the
manager refreshes every 60 s; the API hands those rows out unchanged. A second
read-only DB role (`dashboard_reader`) ensures the dashboard cannot write to the
DB even by accident.

## Deliverables — server

- `src/manager/dashboard/api.py` — FastAPI `APIRouter`. All endpoints GET, JSON,
  read-only. Set `Cache-Control` + `ETag` based on source-row `snapshot_at`/`ts`;
  `If-None-Match` returns 304 cheaply.

  | Path | Returns | Source |
  |---|---|---|
  | `/api/health` | `{healthy, postgres_ok, all_bots_alive}` | manager + PG ping |
  | `/api/status` | per-bot live status (heartbeat age, mode, last error) + counts | in-process manager state |
  | `/api/bots` | latest snapshot per bot | `bot_state` |
  | `/api/bots/{id}` | drill-down + last 50 audit rows | `bot_state` + `audit_log` |
  | `/api/strategies` | per-strategy roll-up: wins, losses, pnl, n_bots, n_markets | `perf_rollup` |
  | `/api/strategies/{name}` | per-bot breakdown for one strategy | `perf_rollup` + `bot_state` |
  | `/api/markets` | per-market exposure, pnl, n_bots | `perf_rollup` |
  | `/api/audit` | paginated `audit_log` query w/ filters | `audit_log` |
  | `/api/failures` | last-7-day timeline: trips, crashes, rejection bursts, stale signals | `audit_log` + respawn log |
  | `/api/capital` | total + per-exchange balance | `bot_state.state.balance` (Phase 4 wallet probes later) |

- `src/manager/dashboard/rollup.py` — background task refreshes
  `perf_rollup` every 60 s via `REFRESH MATERIALIZED VIEW CONCURRENTLY`.
- `src/manager/dashboard/server.py` — uvicorn server inside the manager's asyncio
  loop on `127.0.0.1:8090` (configurable). Mounts the API router and serves the
  static SPA. Updates `src/manager/__main__.py` to add `--dashboard-port` and
  start the server alongside the supervisor.

## Deliverables — Postgres

- Alembic migration `<hash>_phase6b_perf_rollup.py`:
  - **Materialized view `perf_rollup`** keyed by `(strategy, market_id, bot_id)`,
    sourced from `audit_log` (kinds `order_filled`, `order_rejected`, `settled`).
    Columns: `wins, losses, gross_pnl, realized_pnl, unrealized_pnl, n_orders,
    last_fill_at`. Indexed on `(strategy)` and `(market_id)`.
  - **Read-only role `dashboard_reader`** with `SELECT` on the relevant
    tables/views; no DML, no DDL. Manager opens a second asyncpg pool with this
    role for the dashboard only.

## Deliverables — client (`web/`)

A separate sub-project that builds to a static bundle. **This is the heavy side.**

- Stack: Vite + React + TypeScript + TanStack Query + Recharts (or
  lightweight-charts) + Tailwind. No SSR.
- Routes: `/` overview, `/bots`, `/strategies`, `/markets`, `/audit`, `/failures`.
- Refresh cadence (controlled in browser; visibility-aware):
  - 2 s — `/api/status`
  - 60 s — `/api/strategies`, `/api/markets`
  - on-demand — `/api/audit`
- All charts, filters, sorts computed in the browser.
- Output `web/dist/` copied into the manager image at `/app/web/dist/`; FastAPI
  `StaticFiles` serves it.
- Bundle size budget: ≤ 250 KB gzipped (CI gate).

## Deliverables — auth / exposure

- v1: bind to `127.0.0.1` only. Operator gets in via Tailscale or
  `ssh -L 8090:localhost:8090 vps`.
- No app-level auth in v1. A `# TODO(phase-9)` marker reserves the spot for
  bearer-token / OIDC without restructuring the routes.
- Public exposure is a Phase 9 task — needs a real auth story before it ships.

## Deliverables — CI / build

- `.github/workflows/web.yml`: `npm ci && npm run build && npm test`; publishes
  the built bundle as a workflow artifact.
- `docker/Dockerfile.manager` gains a multi-stage `COPY --from=...` for the
  bundle. No npm in the runtime image.
- Bundle size CI gate.

## Deliverables — tests

Server:
- `tests/unit/test_dashboard_api.py` — FastAPI `TestClient`; per-endpoint JSON
  shape + ETag/304 behavior.
- `tests/integration/test_perf_rollup.py` — seed `audit_log` with a known
  scenario, refresh the view, assert aggregates.
- `tests/unit/test_dashboard_readonly_role.py` — connecting as
  `dashboard_reader` and attempting an UPDATE must fail.

Client:
- Vitest unit tests for chart-data transforms.
- Playwright smoke test against a recorded JSON fixture: each route renders
  without console errors.

## Deliverables — docs / rules

- `.claude/rules/06b-dashboard.md` (new) — invariants:
  1. Dashboard NEVER writes to the DB; enforced at the PG role level.
  2. All aggregation lives in materialized views or in the client. No ad-hoc
     `SELECT ... GROUP BY` in handlers — that's what slows the VPS down.
  3. Static bundle ships in the manager image; no CDN fetch at runtime.
  4. v1 is `127.0.0.1`-only; public exposure is a Phase 9 decision.
- Update `plan/06a-observability.md` to note Grafana is now optional and
  cross-reference this phase.
- Update `README.md` with a "Dashboard" section + Tailscale/SSH tunnel
  instructions.

## Hard rules

- **No state mutation from the dashboard.** The PG role enforces it; tests prove
  it. Even an attack against the API surface cannot trip the kill switch, write
  audit rows, or touch snapshots.
- **No new ad-hoc aggregation in API handlers.** If a new view is needed, add a
  new materialized view + a new endpoint. This is the only way to keep the VPS
  unloaded as the dashboard grows.
- **The redaction filter (Phase 3) is applied to any free-form text** (`last_error`,
  stack-trace tails) before it leaves the API. A stack trace cannot leak a secret
  through this surface.

## Exit criteria

- [ ] `/api/status` median response time < 200 ms with `n_bots = 10`.
- [ ] Phone over Tailscale renders the bot list + a strategy drill-down with VPS
      manager CPU ≤ 5% above baseline during 5 minutes of active browsing.
- [ ] `dashboard_reader` cannot execute INSERT / UPDATE / DELETE — verified by
      test.
- [ ] `perf_rollup` refreshes every minute and survives a manager restart.
- [ ] Killing a bot is visible in the UI within 5 s; the failure appears in
      `/failures` within 60 s.
- [ ] Static bundle ≤ 250 KB gzipped.

## Out of scope (deferred)

- Public internet exposure with auth (Phase 9).
- Server-side rendering / SSR.
- Push-based updates (WebSocket / SSE). v1 polls on a timer; revisit only if
  the 2 s status cadence proves visibly laggy.
- Multi-tenant or multi-operator views. Single operator assumed.
- Editing config or tripping the kill switch from the UI — read-only by
  design. Mutation tools stay CLI-only.

## Open questions

- Chart library: Recharts vs lightweight-charts. Recharts is more flexible;
  lightweight-charts is smaller and better for time series. Decide after
  prototyping the strategies view against the bundle-size budget.
- Capital view: how do we get authoritative wallet balance pre-Phase-4? Best
  current source is the latest snapshot's `state.balance` — accept that it
  trails the exchange by one tick until Phase 4 wires the wallet probe.
