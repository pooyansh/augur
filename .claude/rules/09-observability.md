# Rule 09 — Observability (Phase 6a)

## Cardinality discipline

**bot_id** and **strategy** are approved label dimensions.
**market_id is NOT a label** — it has unbounded cardinality as markets proliferate.

Adding a new label requires:
1. Updating this file with the rationale.
2. Updating the Grafana dashboards in `ops/grafana/` in the same PR.
3. Orphan metrics (defined in `src/observability/metrics.py` but absent from dashboards) are a code smell and must be resolved before merge.

## /metrics endpoint

- Lives at `GET /api/metrics` on the dashboard FastAPI app (port 8090).
- Bound to `127.0.0.1` only in v1 — **never exposed publicly without Phase 9 auth**.
- Prometheus scrapes via an SSH tunnel (`ssh -L 9090:127.0.0.1:8090 user@vps`).
- No ETag or Cache-Control — Prometheus expects a fresh response on every scrape.
- No mutation via this endpoint — read-only.

## /healthz endpoint

- Lives at `GET /api/healthz` on the dashboard FastAPI app (port 8090).
- Returns **200** when:
  - Postgres `SELECT 1` succeeds.
  - All supervised bot heartbeats are within the configured tolerance (default 60 s).
- Returns **503** with the same JSON body shape when either check fails.
- Response body is bounded — one entry per bot, no free-form fields.
- No secrets, no free-form log output in the response body.

## Required log fields

Every structured log line MUST contain:
- `ts` — ISO 8601 UTC timestamp.
- `level` — log level string.
- `event` — human-readable event description.
- `bot_id` — present when in a bot context (may be absent in manager-level lines).
- `tick_id` — present when inside a tick (set by `bind_tick_id`); absent between ticks.

## tick_id as correlation id

`tick_id` is generated at the start of each tick by `BaseBot.run` using:
```python
blake2s(f"{bot_id}:{intent_seq}:{tick_start_iso}".encode(), digest_size=8).hexdigest()
```

It is propagated via:
- `src/observability/context.tick_id_var` (ContextVar).
- structlog's bound contextvars (so all log lines within `bind_tick_id` carry it).
- `AuditLogger.write` reads it from the contextvar and stamps `payload._tick_id`.
- Alert payloads should include `tick_id` when originating from a tick context.

## Adding a metric

1. Define it in `src/observability/metrics.py` in the central `REGISTRY`.
2. Add the corresponding Prometheus query to both Grafana dashboards.
3. Update the SLO in `ops/SLOs.md` if the metric backs a budget.
4. Add a unit test in `tests/unit/test_observability_metrics.py`.
5. Update this file if the metric introduces a new label dimension.

## Log shipping

File-based logs are written to `LOG_DIR` (default `/var/log/manager/`).
Rotated by the `logrotate` config at `ops/logrotate/manager.conf`.
Remote shipping (Loki, S3) is a Phase 9+ decision — do not implement here.
