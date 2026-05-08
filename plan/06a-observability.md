# Phase 6a — Observability baseline

**Goal:** Make the platform legible from outside the process. Alerts (Phase 6) tell you when something is broken; observability tells you what's happening when nothing is alerting. You cannot operate live without it.

Land before Phase 7's paper run, not after — the paper run is the first time you'll wish you had it.

## Deliverables — metrics

- Prometheus client embedded in the manager and every bot. Manager scrapes a per-bot endpoint over the unix socket; exports a single aggregated `/metrics` endpoint.
- Core metrics (low cardinality — `bot_id` is a label, market id is **not** unless capped):
  - `bot_tick_latency_seconds` (histogram) — time from tick start to `on_tick` return.
  - `bot_tick_overrun_total` (counter) — ticks that ran longer than the schedule interval.
  - `bot_snapshot_lag_seconds` (gauge) — wall-clock age of the latest committed snapshot.
  - `bot_heartbeat_age_seconds` (gauge) — time since manager last received a heartbeat.
  - `order_intent_total{result=accepted|rejected|risk_blocked|kill_switch}` (counter).
  - `order_fill_latency_seconds` (histogram) — intent submitted → fill observed.
  - `signal_fetch_total{signal,source,result}` and `signal_staleness_seconds{signal}`.
  - `exchange_rate_limit_remaining{exchange}` — if the exchange exposes it.
  - `wallet_allowance_ratio{exchange}` — current on-chain allowance as a fraction of the configured cap (Phase 4 wallet safety). Drift here is the early warning of a misconfigured float.
  - `pnl_realized_usd{bot_id}`, `pnl_unrealized_usd{bot_id}`, `position_notional_usd{bot_id}`.
- A Grafana dashboard JSON checked into `ops/grafana/`. One bot-overview panel, one per-bot drill-down panel.

## Deliverables — structured logs

- JSON logs by default, one event per line. Required fields: `ts` (UTC), `level`, `bot_id`, `tick_id`, `event`, plus event-specific fields.
- `tick_id` is generated at tick start and threaded through every log line and order intent for that tick — it's the correlation id.
- Log shipping is **not** in scope here. File-based logs to a bind-mounted directory, rotated by `logrotate` on the host. Shipping (Loki, S3, etc.) is a Phase 9+ decision.
- The redaction filter (Phase 3) is mandatory on the JSON encoder.

## Deliverables — health & SLOs

- `/healthz` endpoint on the manager: returns 200 only if all bots' heartbeats are within their tolerance and Postgres is reachable.
- A short `ops/SLOs.md` documenting:
  - Tick on-time rate target (e.g. ≥99% of ticks complete within their schedule interval).
  - Snapshot lag target (e.g. p99 < 60 seconds).
  - Order placement success rate target (excluding deliberate risk blocks).
- These are budgets, not contracts. Missing them prompts investigation; they don't page.

## Out of scope (deliberately)

- Distributed tracing (OpenTelemetry). One process tree on one VPS does not need it. Revisit if/when the architecture goes multi-host.
- Centralized log aggregation. Defer until either the VPS count > 1 or local `grep` becomes painful.
- APM products. Same reasoning.

## Exit criteria

- [ ] All metrics above are visible in the local Grafana stack within 60 seconds of being emitted.
- [ ] A staged failure (kill a bot, stale a signal, trip a risk cap) shows up in the dashboard before — or at the same time as — the corresponding alert.
- [ ] No log line, ever, contains a value present in the loaded secrets (verified by a property test that injects each secret value into a log call and asserts it's redacted).
