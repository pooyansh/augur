# Rule 06b — Operator Dashboard

## Invariants

1. **Dashboard NEVER writes to the DB.**
   Enforced at two levels:
   - Postgres role: `dashboard_reader` has `SELECT` only; `INSERT/UPDATE/DELETE`
     raise `InsufficientPrivilegeError` at the driver level.
   - Code: no `POST/PUT/PATCH/DELETE` endpoints exist. The API router is
     `GET`-only. Tests in `tests/integration/test_dashboard_readonly_role.py`
     prove the role-level enforcement.

1a. **Invariant 1 is a database invariant; one narrow, deliberate exception exists.**
   `POST /api/control/bots/{bot_id}/stop` is the *only* non-GET route in the
   entire dashboard surface. It is a same-process call directly into
   `Supervisor.stop_bot()` — it touches zero Postgres rows, so it does not
   violate "dashboard never writes to the DB" (that invariant is specifically
   about the database, not about HTTP verbs generally). It is:
   - **Separately routed**: `/api/control/...`, its own file
     (`src/manager/dashboard/control_api.py`), never added to the read-only
     `router`/`ops_router` in `api.py`.
   - **Audit-logged**: every invocation writes a `KIND_BOT_STOP_REQUESTED`
     row via the same `AuditLogger` instance used elsewhere in the manager
     process, before the HTTP response returns.
   - **Loopback-only**: bound via the same `DashboardServer.start(host, port)`
     call as everything else — no separate bind, no exception to invariant 4.

   This is the *only* write-like route and must stay that way. Any future
   dashboard feature that wants to mutate anything needs its own explicit
   justification here — this exception is not precedent for adding more.

2. **All aggregation lives in materialized views or in the client.**
   API handlers MUST NOT run ad-hoc `SELECT ... GROUP BY` queries against raw
   tables. If a new aggregation is needed, add a new materialized view
   (+ Alembic migration) and a new endpoint backed by that view. This keeps
   the VPS unloaded as the dashboard grows.

3. **Static bundle ships in the manager image; no CDN fetch at runtime.**
   `web/dist/` is `COPY`-ed into the image via a multi-stage build. The SPA
   never fetches from an external CDN when the manager container starts. All
   assets are served from the same origin as the API.

4. **v1 is `127.0.0.1`-only.**
   The `--dashboard-host` CLI flag rejects any value other than `127.0.0.1`
   with a clear error and exits non-zero. Public exposure is a Phase 9
   decision — it requires a proper auth story (bearer-token / OIDC).

   ```python
   # TODO(phase-9): add bearer-token / OIDC auth before allowing non-loopback
   # bind addresses.
   ```

5. **Redaction is mandatory on all free-form text.**
   Any field that can carry user/exchange-supplied strings (`last_error`,
   stack-trace tails, payload echoes) MUST be passed through `JsonRedactor`
   before serialization. The same `RedactionFilter` from Phase 3 backs this.
   A stack trace MUST NOT leak a secret value through the dashboard API.

6. **ETag + Cache-Control on every GET endpoint.**
   - ETag format: `W/"<table>-<max_ts.isoformat()>"`.
   - Return 304 if `If-None-Match` matches.
   - `Cache-Control: no-store` so browsers always revalidate.

7. **perf_rollup refreshes every 60 s.**
   `PerfRollupRefresher` runs `REFRESH MATERIALIZED VIEW CONCURRENTLY perf_rollup`
   in a background asyncio task. A unique index on `(strategy, market_id, bot_id)`
   is required for `CONCURRENTLY`. Refresh failures log at `warn` and never
   propagate.

8. **Two asyncpg pools, separate roles.**
   - Write pool: supervisor / repositories — uses `POSTGRES_USER`.
   - Read-only pool: dashboard — uses `DASHBOARD_PG_USER` (dashboard_reader).
   If `DASHBOARD_PG_USER` is not set, falls back to the write-role creds with
   a `warning` log.

9. **Bundle size budget: ≤ 250 KB gzipped.**
   CI gates on this. If a new chart library or utility pushes the bundle over
   the limit, use code splitting or find a lighter alternative before merging.

## Endpoint contract summary

| Path | Source | Notes |
|---|---|---|
| `/api/health` | supervisor + `DashboardDb.ping()` | No DB query on healthy path |
| `/api/status` | supervisor in-process | Never hits DB |
| `/api/bots` | `bot_state` | Latest snapshot per bot |
| `/api/bots/{id}` | `bot_state` + `audit_log` | Last 50 audit rows |
| `/api/strategies` | `perf_rollup` | Aggregated by strategy |
| `/api/strategies/{name}` | `perf_rollup` | Per-bot breakdown |
| `/api/markets` | `perf_rollup` | Aggregated by market_id |
| `/api/audit` | `audit_log` | Paginated, filtered |
| `/api/failures` | `audit_log` | Last 7 days, failure kinds only |
| `/api/capital` | `bot_state.state.balance` | Phase 4 wallet probe pending |
| `POST /api/control/bots/{id}/stop` | `Supervisor.stop_bot()` | The sole non-GET route — see invariant 1a |

## Refresh cadence (client-side, visibility-aware)

| Endpoint | Interval | Notes |
|---|---|---|
| `/api/status` | 2 s | Pause when `document.hidden` |
| `/api/strategies`, `/api/markets`, `/api/bots` | 60 s | Pause when hidden |
| `/api/audit`, `/api/failures` | on-demand | User-triggered |
