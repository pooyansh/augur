"""Pydantic response models for the dashboard API.

All models must exactly match the JSON contract in ``plan/06b-dashboard.md``.
``response_model_exclude_none=True`` is applied on every endpoint — include a
field only when you actually have data for it.
"""

from __future__ import annotations

__all__ = [
    "AuditRow",
    "BotDetail",
    "BotSummary",
    "CapitalResponse",
    "ExchangeBalance",
    "FailureEvent",
    "FailuresResponse",
    "HealthResponse",
    "MarketExposure",
    "MarketsResponse",
    "StatusBot",
    "StatusResponse",
    "StrategiesResponse",
    "StrategyDetail",
    "StrategyRollup",
    "StrategySummary",
]

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    """Response model for ``GET /api/health``."""

    model_config = ConfigDict(populate_by_name=True)

    healthy: bool
    postgres_ok: bool
    all_bots_alive: bool


class StatusBot(BaseModel):
    """Per-bot entry in the ``GET /api/status`` response."""

    model_config = ConfigDict(populate_by_name=True)

    bot_id: str
    strategy: str
    mode: str
    pid: int | None
    restart_count: int
    heartbeat_age_s: float
    snapshot_lag_s: float
    last_error: str | None
    spawned_at: datetime


class StatusResponse(BaseModel):
    """Response model for ``GET /api/status``."""

    model_config = ConfigDict(populate_by_name=True)

    bots: list[StatusBot]
    total_bots: int
    alive_bots: int
    paper_bots: int
    live_bots: int


class BotSummary(BaseModel):
    """One entry in the ``GET /api/bots`` list — latest snapshot per bot."""

    model_config = ConfigDict(populate_by_name=True)

    bot_id: str
    strategy: str
    market_id: str
    mode: str
    snapshot_at: datetime
    version: int
    state: dict[str, Any]


class AuditRow(BaseModel):
    """One row from ``audit_log`` — used in ``/api/audit`` and ``/api/bots/{id}``."""

    model_config = ConfigDict(populate_by_name=True)

    id: int
    ts: datetime
    bot_id: str
    kind: str
    client_order_id: str | None
    exchange_order_id: str | None
    payload: dict[str, Any]


class BotDetail(BaseModel):
    """Response model for ``GET /api/bots/{id}``."""

    model_config = ConfigDict(populate_by_name=True)

    bot_id: str
    strategy: str
    market_id: str
    mode: str
    snapshot_at: datetime
    version: int
    state: dict[str, Any]
    recent_audit: list[AuditRow]


class StrategyRollup(BaseModel):
    """Aggregated metrics for one strategy from ``perf_rollup``."""

    model_config = ConfigDict(populate_by_name=True)

    strategy: str
    wins: int
    losses: int
    gross_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    n_orders: int
    n_bots: int
    n_markets: int
    last_fill_at: datetime | None


class StrategiesResponse(BaseModel):
    """Response model for ``GET /api/strategies``."""

    model_config = ConfigDict(populate_by_name=True)

    strategies: list[StrategyRollup]


class StrategyBotBreakdown(BaseModel):
    """Per-bot row for a single strategy drill-down."""

    model_config = ConfigDict(populate_by_name=True)

    bot_id: str
    market_id: str
    wins: int
    losses: int
    gross_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    n_orders: int
    last_fill_at: datetime | None


class StrategySummary(BaseModel):
    """Aggregated row for strategy listing — single strategy entry."""

    model_config = ConfigDict(populate_by_name=True)

    strategy: str
    wins: int
    losses: int
    gross_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    n_orders: int
    n_bots: int
    n_markets: int
    last_fill_at: datetime | None


class StrategyDetail(BaseModel):
    """Response model for ``GET /api/strategies/{name}``."""

    model_config = ConfigDict(populate_by_name=True)

    strategy: str
    summary: StrategySummary
    bots: list[StrategyBotBreakdown]


class MarketExposure(BaseModel):
    """Aggregated metrics for one market from ``perf_rollup``."""

    model_config = ConfigDict(populate_by_name=True)

    market_id: str
    gross_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    n_bots: int
    n_orders: int
    last_fill_at: datetime | None


class MarketsResponse(BaseModel):
    """Response model for ``GET /api/markets``."""

    model_config = ConfigDict(populate_by_name=True)

    markets: list[MarketExposure]


class FailureEvent(BaseModel):
    """One failure event in the last-7-day timeline."""

    model_config = ConfigDict(populate_by_name=True)

    ts: datetime
    bot_id: str
    kind: str
    detail: str | None


class FailuresResponse(BaseModel):
    """Response model for ``GET /api/failures``."""

    model_config = ConfigDict(populate_by_name=True)

    events: list[FailureEvent]
    total: int


class ExchangeBalance(BaseModel):
    """Balance for one exchange from snapshot state."""

    model_config = ConfigDict(populate_by_name=True)

    exchange: str
    balance: float
    currency: str


class CapitalResponse(BaseModel):
    """Response model for ``GET /api/capital``."""

    model_config = ConfigDict(populate_by_name=True)

    total_usd: float
    per_exchange: list[ExchangeBalance]
    sourced_from: str
