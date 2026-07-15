"""Dashboard FastAPI router — read-only JSON endpoints.

All endpoints are GET.  No POST/PUT/PATCH/DELETE anywhere in this module.
Auth is deferred to Phase 9; v1 is bound to ``127.0.0.1`` only.

ETag + Cache-Control are set on every response:
    - ETag value: ``W/"<table>-<max_ts.isoformat()>"``
    - If the client sends ``If-None-Match`` matching the current ETag, the
      handler returns 304 with no body.
    - Cache-Control: ``no-store`` (ensures browsers always revalidate).

# TODO(phase-9): Add bearer-token / OIDC auth here when public exposure lands.
"""

from __future__ import annotations

__all__ = ["make_router"]

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from src.manager.dashboard.db import DashboardDb
from src.manager.dashboard.models import (
    AuditRow,
    BotDetail,
    BotSummary,
    CapitalResponse,
    ExchangeBalance,
    FailureEvent,
    FailuresResponse,
    HealthResponse,
    MarketExposure,
    MarketsResponse,
    StatusBot,
    StatusResponse,
    StrategiesResponse,
    StrategyBotBreakdown,
    StrategyDetail,
    StrategyRollup,
    StrategySummary,
)
from src.manager.dashboard.redact import JsonRedactor

logger = logging.getLogger(__name__)


def _make_etag(table: str, ts: datetime | None) -> str:
    """Build a weak ETag string.

    Args:
        table: Source table/view name.
        ts: Most recent timestamp from the source (may be None for empty tables).

    Returns:
        Weak ETag value, e.g. ``W/"bot_state-2026-05-09T12:00:00+00:00"``.
    """
    ts_str = ts.isoformat() if ts is not None else "empty"
    return f'W/"{table}-{ts_str}"'


def _check_304(request: Request, etag: str) -> bool:
    """Return True if the client's If-None-Match header matches the ETag.

    Args:
        request: Incoming FastAPI request.
        etag: Current ETag value.

    Returns:
        True when 304 should be returned.
    """
    client_etag = request.headers.get("if-none-match")
    return client_etag == etag


def _set_cache_headers(response: Response, etag: str) -> None:
    """Apply ETag and Cache-Control headers to the response.

    Args:
        response: FastAPI Response object.
        etag: ETag value to set.
    """
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"


def _parse_payload(raw: Any) -> dict[str, Any]:
    """Parse a payload field that may be a string or dict.

    asyncpg returns JSONB as a dict in most drivers; protect against strings.

    Args:
        raw: Raw payload value from asyncpg.

    Returns:
        Dict representation.
    """
    if isinstance(raw, str):
        return json.loads(raw)  # type: ignore[no-any-return]
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _parse_state(raw: Any) -> dict[str, Any]:
    """Parse a state JSONB column that may be a string or dict.

    Args:
        raw: Raw state value from asyncpg.

    Returns:
        Dict representation.
    """
    return _parse_payload(raw)


def make_router(
    db: DashboardDb,
    redactor: JsonRedactor,
    supervisor: Any | None = None,
    health_checker: Any | None = None,
) -> tuple[APIRouter, APIRouter]:
    """Build and return the dashboard routers.

    Args:
        db: Opened DashboardDb instance for read-only queries.
        redactor: JsonRedactor for scrubbing free-form text fields.
        supervisor: Optional Supervisor instance for /api/status and /api/health;
            if None, those endpoints return minimal responses.
        health_checker: Optional HealthChecker for /healthz; if None, the endpoint
            returns a minimal 200 response.

    Returns:
        Tuple ``(api_router, ops_router)``. ``api_router`` is prefixed ``/api``
        and serves the human-facing dashboard endpoints. ``ops_router`` is
        unprefixed and serves ``/metrics`` (Prometheus convention) and
        ``/healthz`` (machine health check) at the root path.
    """
    router = APIRouter(prefix="/api")
    ops_router = APIRouter()

    # ------------------------------------------------------------------
    # GET /metrics  (Prometheus scrape endpoint — no ETag, no Cache-Control)
    # Mounted at root (not /api) because Prometheus scrape configs expect
    # /metrics by convention.
    # ------------------------------------------------------------------

    @ops_router.get("/metrics")
    async def metrics_endpoint() -> Response:
        """Expose Prometheus metrics in the text exposition format.

        ETag is intentionally omitted — Prometheus expects a fresh response on
        every scrape and does not send If-None-Match.

        Returns:
            Prometheus text format body with the correct Content-Type header.
        """
        try:
            from src.observability.metrics import render_metrics

            body, content_type = render_metrics()
            return Response(content=body, media_type=content_type)
        except Exception as exc:
            logger.error("Failed to render Prometheus metrics: %s", exc)
            return Response(content=b"# metrics unavailable\n", media_type="text/plain")

    # ------------------------------------------------------------------
    # GET /healthz  (machine-grade health check — root path, not /api)
    # ------------------------------------------------------------------

    @ops_router.get("/healthz")
    async def healthz_endpoint() -> Response:
        """Return 200 when healthy, 503 when unhealthy.

        Checks:
        - Postgres is reachable (SELECT 1).
        - All bot heartbeats are within tolerance.

        The JSON body has the same shape regardless of status code so callers
        can inspect which checks failed.

        Returns:
            JSON body with healthy, postgres_ok, bots list, and ts.
        """
        import json as _json

        if health_checker is not None:
            try:
                report = await health_checker.check()
            except Exception as exc:
                logger.error("HealthChecker.check() failed: %s", exc)
                report = {
                    "healthy": False,
                    "postgres_ok": False,
                    "bots": [],
                    "ts": datetime.now(UTC).isoformat(),
                }
        else:
            # No health checker configured — return a minimal healthy response.
            report = {
                "healthy": True,
                "postgres_ok": False,
                "bots": [],
                "ts": datetime.now(UTC).isoformat(),
            }

        status_code = 200 if report.get("healthy") else 503
        return Response(
            content=_json.dumps(report),
            media_type="application/json",
            status_code=status_code,
        )

    # ------------------------------------------------------------------
    # GET /api/health
    # ------------------------------------------------------------------

    @router.get(
        "/health",
        response_model=HealthResponse,
        response_model_exclude_none=True,
    )
    async def health(request: Request, response: Response) -> HealthResponse:
        """Check manager + Postgres health.

        Returns:
            HealthResponse with healthy, postgres_ok, all_bots_alive flags.
        """
        etag = _make_etag("health", datetime.now(UTC))
        if _check_304(request, etag):
            return Response(status_code=304)  # type: ignore[return-value]

        postgres_ok = await db.ping()
        alive_bots = True
        if supervisor is not None:
            statuses = supervisor.status()
            alive_bots = all(s.pid is not None for s in statuses) if statuses else True

        result = HealthResponse(
            healthy=postgres_ok,
            postgres_ok=postgres_ok,
            all_bots_alive=alive_bots,
        )
        _set_cache_headers(response, etag)
        return result

    # ------------------------------------------------------------------
    # GET /api/status
    # ------------------------------------------------------------------

    @router.get(
        "/status",
        response_model=StatusResponse,
        response_model_exclude_none=True,
    )
    async def status(request: Request, response: Response) -> StatusResponse:
        """Return per-bot live status from the in-process supervisor.

        Returns:
            StatusResponse with per-bot heartbeat age, mode, last_error, etc.
        """
        etag = _make_etag("status", datetime.now(UTC))
        if _check_304(request, etag):
            return Response(status_code=304)  # type: ignore[return-value]

        if supervisor is None:
            result = StatusResponse(
                bots=[],
                total_bots=0,
                alive_bots=0,
                paper_bots=0,
                live_bots=0,
            )
            _set_cache_headers(response, etag)
            return result

        statuses = supervisor.status()
        bots: list[StatusBot] = []
        for s in statuses:
            bots.append(
                StatusBot(
                    bot_id=s.bot_id,
                    strategy=s.strategy,
                    mode=s.mode,
                    pid=s.pid,
                    restart_count=s.restart_count,
                    heartbeat_age_s=s.heartbeat_age_s,
                    snapshot_lag_s=s.snapshot_lag_s,
                    last_error=redactor.redact_string(s.last_error),
                    spawned_at=s.spawned_at,
                )
            )

        alive = sum(1 for b in bots if b.pid is not None)
        paper = sum(1 for b in bots if b.mode == "paper")
        live = sum(1 for b in bots if b.mode == "live")

        result = StatusResponse(
            bots=bots,
            total_bots=len(bots),
            alive_bots=alive,
            paper_bots=paper,
            live_bots=live,
        )
        _set_cache_headers(response, etag)
        return result

    # ------------------------------------------------------------------
    # GET /api/bots
    # ------------------------------------------------------------------

    @router.get(
        "/bots",
        response_model=list[BotSummary],
        response_model_exclude_none=True,
    )
    async def bots_list(request: Request, response: Response) -> list[BotSummary]:
        """Return the latest snapshot per bot.

        Returns:
            List of BotSummary — one per bot in bot_state.
        """
        max_ts = await db.max_snapshot_at()
        etag = _make_etag("bot_state", max_ts)
        if _check_304(request, etag):
            return Response(status_code=304)  # type: ignore[return-value]

        rows = await db.fetch_all_bot_states()
        result = [
            BotSummary(
                bot_id=r["bot_id"],
                strategy=r.get("strategy") or "",
                market_id=r["market_id"],
                mode=r.get("mode") or "paper",
                snapshot_at=r["snapshot_at"],
                version=r["version"],
                state=redactor.redact(_parse_state(r["state"])),
            )
            for r in rows
        ]
        _set_cache_headers(response, etag)
        return result

    # ------------------------------------------------------------------
    # GET /api/bots/{id}
    # ------------------------------------------------------------------

    @router.get(
        "/bots/{bot_id}",
        response_model=BotDetail,
        response_model_exclude_none=True,
    )
    async def bot_detail(
        bot_id: str,
        request: Request,
        response: Response,
    ) -> BotDetail:
        """Return snapshot + last 50 audit rows for one bot.

        Args:
            bot_id: Stable bot identifier.

        Returns:
            BotDetail with snapshot and recent_audit list.

        Raises:
            HTTPException: 404 if bot_id not found.
        """
        max_ts = await db.max_snapshot_at()
        etag = _make_etag(f"bot_state-{bot_id}", max_ts)
        if _check_304(request, etag):
            return Response(status_code=304)  # type: ignore[return-value]

        row = await db.fetch_bot_state(bot_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Bot '{bot_id}' not found.")

        audit_rows = await db.fetch_bot_audit(bot_id, limit=50)
        state = _parse_state(row["state"])

        result = BotDetail(
            bot_id=row["bot_id"],
            strategy=row.get("strategy") or "",
            market_id=row["market_id"],
            mode=row.get("mode") or "paper",
            snapshot_at=row["snapshot_at"],
            version=row["version"],
            state=redactor.redact(state),
            recent_audit=[
                AuditRow(
                    id=a["id"],
                    ts=a["ts"],
                    bot_id=a["bot_id"],
                    kind=a["kind"],
                    client_order_id=a.get("client_order_id"),
                    exchange_order_id=a.get("exchange_order_id"),
                    payload=redactor.redact(_parse_payload(a["payload"])),
                )
                for a in audit_rows
            ],
        )
        _set_cache_headers(response, etag)
        return result

    # ------------------------------------------------------------------
    # GET /api/strategies
    # ------------------------------------------------------------------

    @router.get(
        "/strategies",
        response_model=StrategiesResponse,
        response_model_exclude_none=True,
    )
    async def strategies(request: Request, response: Response) -> StrategiesResponse:
        """Return per-strategy roll-up from perf_rollup.

        Returns:
            StrategiesResponse with wins, losses, pnl, n_bots, n_markets per strategy.
        """
        max_ts = await db.max_perf_rollup_fill()
        etag = _make_etag("perf_rollup", max_ts)
        if _check_304(request, etag):
            return Response(status_code=304)  # type: ignore[return-value]

        rows = await db.fetch_strategy_rollups()
        result = StrategiesResponse(
            strategies=[
                StrategyRollup(
                    strategy=r["strategy"],
                    wins=int(r["wins"] or 0),
                    losses=int(r["losses"] or 0),
                    gross_pnl=float(r["gross_pnl"] or 0),
                    realized_pnl=float(r["realized_pnl"] or 0),
                    unrealized_pnl=float(r["unrealized_pnl"] or 0),
                    n_orders=int(r["n_orders"] or 0),
                    n_bots=int(r["n_bots"] or 0),
                    n_markets=int(r["n_markets"] or 0),
                    last_fill_at=r.get("last_fill_at"),
                )
                for r in rows
            ]
        )
        _set_cache_headers(response, etag)
        return result

    # ------------------------------------------------------------------
    # GET /api/strategies/{name}
    # ------------------------------------------------------------------

    @router.get(
        "/strategies/{name}",
        response_model=StrategyDetail,
        response_model_exclude_none=True,
    )
    async def strategy_detail(
        name: str,
        request: Request,
        response: Response,
    ) -> StrategyDetail:
        """Return per-bot breakdown for one strategy.

        Args:
            name: Strategy name.

        Returns:
            StrategyDetail with summary + per-bot breakdown.

        Raises:
            HTTPException: 404 if strategy not found.
        """
        max_ts = await db.max_perf_rollup_fill()
        etag = _make_etag(f"perf_rollup-{name}", max_ts)
        if _check_304(request, etag):
            return Response(status_code=304)  # type: ignore[return-value]

        bot_rows = await db.fetch_strategy_detail(name)
        if not bot_rows:
            raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found.")

        total_wins = sum(int(r["wins"] or 0) for r in bot_rows)
        total_losses = sum(int(r["losses"] or 0) for r in bot_rows)
        total_gross = sum(float(r["gross_pnl"] or 0) for r in bot_rows)
        total_realized = sum(float(r["realized_pnl"] or 0) for r in bot_rows)
        total_unrealized = sum(float(r["unrealized_pnl"] or 0) for r in bot_rows)
        total_orders = sum(int(r["n_orders"] or 0) for r in bot_rows)
        last_fill = max(
            (r["last_fill_at"] for r in bot_rows if r.get("last_fill_at") is not None),
            default=None,
        )

        summary = StrategySummary(
            strategy=name,
            wins=total_wins,
            losses=total_losses,
            gross_pnl=total_gross,
            realized_pnl=total_realized,
            unrealized_pnl=total_unrealized,
            n_orders=total_orders,
            n_bots=len(bot_rows),
            n_markets=len({r["market_id"] for r in bot_rows}),
            last_fill_at=last_fill,
        )

        result = StrategyDetail(
            strategy=name,
            summary=summary,
            bots=[
                StrategyBotBreakdown(
                    bot_id=r["bot_id"],
                    market_id=r["market_id"],
                    wins=int(r["wins"] or 0),
                    losses=int(r["losses"] or 0),
                    gross_pnl=float(r["gross_pnl"] or 0),
                    realized_pnl=float(r["realized_pnl"] or 0),
                    unrealized_pnl=float(r["unrealized_pnl"] or 0),
                    n_orders=int(r["n_orders"] or 0),
                    last_fill_at=r.get("last_fill_at"),
                )
                for r in bot_rows
            ],
        )
        _set_cache_headers(response, etag)
        return result

    # ------------------------------------------------------------------
    # GET /api/markets
    # ------------------------------------------------------------------

    @router.get(
        "/markets",
        response_model=MarketsResponse,
        response_model_exclude_none=True,
    )
    async def markets(request: Request, response: Response) -> MarketsResponse:
        """Return per-market exposure and PnL from perf_rollup.

        Returns:
            MarketsResponse with per-market gross_pnl, n_bots, etc.
        """
        max_ts = await db.max_perf_rollup_fill()
        etag = _make_etag("perf_rollup_markets", max_ts)
        if _check_304(request, etag):
            return Response(status_code=304)  # type: ignore[return-value]

        rows = await db.fetch_market_exposures()
        result = MarketsResponse(
            markets=[
                MarketExposure(
                    market_id=r["market_id"],
                    gross_pnl=float(r["gross_pnl"] or 0),
                    realized_pnl=float(r["realized_pnl"] or 0),
                    unrealized_pnl=float(r["unrealized_pnl"] or 0),
                    n_bots=int(r["n_bots"] or 0),
                    n_orders=int(r["n_orders"] or 0),
                    last_fill_at=r.get("last_fill_at"),
                )
                for r in rows
            ]
        )
        _set_cache_headers(response, etag)
        return result

    # ------------------------------------------------------------------
    # GET /api/audit
    # ------------------------------------------------------------------

    @router.get(
        "/audit",
        response_model=list[AuditRow],
        response_model_exclude_none=True,
    )
    async def audit(
        request: Request,
        response: Response,
        limit: int = 100,
        before: datetime | None = None,
        bot_id: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
    ) -> list[AuditRow]:
        """Return paginated audit_log rows with optional filters.

        Args:
            limit: Max rows (capped at 200).
            before: Keyset cursor — only rows with ts < before.
            bot_id: Filter to a specific bot.
            kind: Filter to a specific event kind.
            since: Only rows with ts >= since.

        Returns:
            List of AuditRow, newest first.
        """
        limit = min(limit, 200)

        max_ts = await db.max_audit_ts()
        etag = _make_etag("audit_log", max_ts)
        if _check_304(request, etag):
            return Response(status_code=304)  # type: ignore[return-value]

        rows = await db.fetch_audit_page(
            limit=limit,
            before=before,
            bot_id=bot_id,
            kind=kind,
            since=since,
        )
        result = [
            AuditRow(
                id=r["id"],
                ts=r["ts"],
                bot_id=r["bot_id"],
                kind=r["kind"],
                client_order_id=r.get("client_order_id"),
                exchange_order_id=r.get("exchange_order_id"),
                payload=redactor.redact(_parse_payload(r["payload"])),
            )
            for r in rows
        ]
        _set_cache_headers(response, etag)
        return result

    # ------------------------------------------------------------------
    # GET /api/failures
    # ------------------------------------------------------------------

    @router.get(
        "/failures",
        response_model=FailuresResponse,
        response_model_exclude_none=True,
    )
    async def failures(request: Request, response: Response) -> FailuresResponse:
        """Return last-7-day failure timeline.

        Sources: audit_log rows whose ``kind`` is a failure kind
        (order_rejected, bot_crash, bot_cooldown, live_downgrade,
        kill_switch_tripped, signal_stale, snapshot_failed).

        Returns:
            FailuresResponse with events list and total count.
        """
        max_ts = await db.max_audit_ts()
        etag = _make_etag("failures", max_ts)
        if _check_304(request, etag):
            return Response(status_code=304)  # type: ignore[return-value]

        rows = await db.fetch_failures(days=7)
        events: list[FailureEvent] = []
        for r in rows:
            payload = _parse_payload(r["payload"])
            # Extract a human-readable detail string from the payload.
            detail = payload.get("reason") or payload.get("error") or payload.get("message") or None
            events.append(
                FailureEvent(
                    ts=r["ts"],
                    bot_id=r["bot_id"],
                    kind=r["kind"],
                    detail=redactor.redact_string(str(detail)) if detail else None,
                )
            )

        result = FailuresResponse(events=events, total=len(events))
        _set_cache_headers(response, etag)
        return result

    # ------------------------------------------------------------------
    # GET /api/capital
    # ------------------------------------------------------------------

    @router.get(
        "/capital",
        response_model=CapitalResponse,
        response_model_exclude_none=True,
    )
    async def capital(request: Request, response: Response) -> CapitalResponse:
        """Return total capital and per-exchange balances.

        Source: ``bot_state.state.balance`` — trails the exchange by one tick
        until Phase 4 wires the wallet probe.

        Returns:
            CapitalResponse with total_usd, per_exchange, sourced_from.
        """
        max_ts = await db.max_snapshot_at()
        etag = _make_etag("capital", max_ts)
        if _check_304(request, etag):
            return Response(status_code=304)  # type: ignore[return-value]

        rows = await db.fetch_all_bot_states_with_strategy()
        balances: dict[str, float] = {}

        for r in rows:
            state = _parse_state(r["state"])
            bal = state.get("balance")
            exchange = state.get("exchange", "unknown")
            if isinstance(bal, (int, float)):
                balances[exchange] = balances.get(exchange, 0.0) + float(bal)

        per_exchange = [
            ExchangeBalance(exchange=ex, balance=amt, currency="USD")
            for ex, amt in sorted(balances.items())
        ]
        total = sum(b.balance for b in per_exchange)

        result = CapitalResponse(
            total_usd=total,
            per_exchange=per_exchange,
            sourced_from="bot_state.state.balance (Phase 4 wallet probe pending)",
        )
        _set_cache_headers(response, etag)
        return result

    return router, ops_router
