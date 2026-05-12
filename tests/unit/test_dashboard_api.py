"""Unit tests for the dashboard API router.

Uses FastAPI's TestClient (synchronous) with mock DashboardDb and
no real Postgres connection.  Tests:
- Per-endpoint JSON shape.
- ETag → 304 behavior with If-None-Match.
- Redaction applied to free-form text fields.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.manager.dashboard.api import make_router
from src.manager.dashboard.db import DashboardDb
from src.manager.dashboard.redact import JsonRedactor

_UNSET: Any = object()  # sentinel for "not provided" vs explicitly None

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)

_FAKE_BOT_STATE: dict[str, Any] = {
    "bot_id": "bot-001",
    "strategy": "momentum_v1",
    "market_id": "market-abc",
    "snapshot_at": _NOW,
    "version": 42,
    "state": {"mode": "paper", "intent_seq": 7, "position": "0.5"},
}

_FAKE_AUDIT_ROW: dict[str, Any] = {
    "id": 1,
    "ts": _NOW,
    "bot_id": "bot-001",
    "kind": "order_submitted",
    "client_order_id": "cid-001",
    "exchange_order_id": None,
    "payload": {"side": "buy", "price": "0.55"},
}

_FAKE_PERF_ROW: dict[str, Any] = {
    "strategy": "momentum_v1",
    "wins": 3,
    "losses": 1,
    "gross_pnl": 12.50,
    "realized_pnl": 10.00,
    "unrealized_pnl": 2.50,
    "n_orders": 10,
    "n_bots": 1,
    "n_markets": 1,
    "last_fill_at": _NOW,
}

_FAKE_PERF_BOT_ROW: dict[str, Any] = {
    "bot_id": "bot-001",
    "market_id": "market-abc",
    "wins": 3,
    "losses": 1,
    "gross_pnl": 12.50,
    "realized_pnl": 10.00,
    "unrealized_pnl": 2.50,
    "n_orders": 10,
    "last_fill_at": _NOW,
}

_FAKE_MARKET_ROW: dict[str, Any] = {
    "market_id": "market-abc",
    "gross_pnl": 12.50,
    "realized_pnl": 10.00,
    "unrealized_pnl": 2.50,
    "n_bots": 1,
    "n_orders": 10,
    "last_fill_at": _NOW,
}

_FAKE_FAILURE_ROW: dict[str, Any] = {
    "id": 99,
    "ts": _NOW,
    "bot_id": "bot-001",
    "kind": "order_rejected",
    "client_order_id": None,
    "exchange_order_id": None,
    "payload": {"reason": "insufficient_funds", "secret_key": "superSECRET123456"},
}


def _make_mock_db(
    *,
    ping_ok: bool = True,
    bot_states: list[dict[str, Any]] | None = None,
    bot_state: Any = _UNSET,  # use _UNSET to distinguish "not given" from explicit None
    audit_rows: list[dict[str, Any]] | None = None,
    strategy_rollups: list[dict[str, Any]] | None = None,
    strategy_detail_rows: list[dict[str, Any]] | None = None,
    market_rows: list[dict[str, Any]] | None = None,
    audit_page_rows: list[dict[str, Any]] | None = None,
    failure_rows: list[dict[str, Any]] | None = None,
    capital_states: list[dict[str, Any]] | None = None,
    max_snapshot_at: datetime | None = _NOW,
    max_audit_ts: datetime | None = _NOW,
    max_perf_fill: datetime | None = _NOW,
) -> DashboardDb:
    """Build a DashboardDb mock with sensible defaults."""
    db = MagicMock(spec=DashboardDb)
    db.ping = AsyncMock(return_value=ping_ok)
    _all_states = bot_states if bot_states is not None else [_FAKE_BOT_STATE]
    db.fetch_all_bot_states = AsyncMock(return_value=_all_states)
    _bot_state_val = _FAKE_BOT_STATE if bot_state is _UNSET else bot_state
    db.fetch_bot_state = AsyncMock(return_value=_bot_state_val)
    _audit = audit_rows if audit_rows is not None else [_FAKE_AUDIT_ROW]
    db.fetch_bot_audit = AsyncMock(return_value=_audit)
    _rollups = strategy_rollups if strategy_rollups is not None else [_FAKE_PERF_ROW]
    db.fetch_strategy_rollups = AsyncMock(return_value=_rollups)
    _strategy_detail_val = (
        [_FAKE_PERF_BOT_ROW] if strategy_detail_rows is None else strategy_detail_rows
    )
    db.fetch_strategy_detail = AsyncMock(return_value=_strategy_detail_val)
    _mkt = market_rows if market_rows is not None else [_FAKE_MARKET_ROW]
    db.fetch_market_exposures = AsyncMock(return_value=_mkt)
    _audit_page = audit_page_rows if audit_page_rows is not None else [_FAKE_AUDIT_ROW]
    db.fetch_audit_page = AsyncMock(return_value=_audit_page)
    _failures = failure_rows if failure_rows is not None else [_FAKE_FAILURE_ROW]
    db.fetch_failures = AsyncMock(return_value=_failures)
    _capital = capital_states if capital_states is not None else []
    db.fetch_all_bot_states_with_strategy = AsyncMock(return_value=_capital)
    db.max_snapshot_at = AsyncMock(return_value=max_snapshot_at)
    db.max_audit_ts = AsyncMock(return_value=max_audit_ts)
    db.max_perf_rollup_fill = AsyncMock(return_value=max_perf_fill)
    return db  # type: ignore[return-value]


def _make_client(
    db: DashboardDb | None = None,
    supervisor: Any | None = None,
    secret_values: list[str] | None = None,
) -> TestClient:
    """Build a TestClient for the dashboard router."""
    _db = db or _make_mock_db()
    redactor = JsonRedactor(secret_values or [])
    router = make_router(db=_db, redactor=redactor, supervisor=supervisor)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------


def test_health_returns_healthy_when_postgres_ok() -> None:
    client = _make_client()
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is True
    assert body["postgres_ok"] is True
    assert body["all_bots_alive"] is True


def test_health_returns_unhealthy_when_postgres_down() -> None:
    db = _make_mock_db(ping_ok=False)
    client = _make_client(db=db)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is False
    assert body["postgres_ok"] is False


def test_health_has_etag_header() -> None:
    client = _make_client()
    resp = client.get("/api/health")
    assert "etag" in resp.headers


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------


def test_status_without_supervisor_returns_empty() -> None:
    client = _make_client(supervisor=None)
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_bots"] == 0
    assert body["bots"] == []


def test_status_with_supervisor_returns_bot_list() -> None:
    mock_bot = MagicMock()
    mock_bot.bot_id = "bot-001"
    mock_bot.strategy = "momentum_v1"
    mock_bot.mode = "paper"
    mock_bot.pid = 12345
    mock_bot.restart_count = 0
    mock_bot.heartbeat_age_s = 5.0
    mock_bot.snapshot_lag_s = 10.0
    mock_bot.last_error = None
    mock_bot.spawned_at = _NOW

    supervisor = MagicMock()
    supervisor.status.return_value = [mock_bot]

    client = _make_client(supervisor=supervisor)
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_bots"] == 1
    assert body["alive_bots"] == 1
    assert body["paper_bots"] == 1
    assert body["live_bots"] == 0
    assert body["bots"][0]["bot_id"] == "bot-001"
    assert body["bots"][0]["strategy"] == "momentum_v1"


def test_status_redacts_last_error() -> None:
    mock_bot = MagicMock()
    mock_bot.bot_id = "bot-001"
    mock_bot.strategy = "momentum_v1"
    mock_bot.mode = "paper"
    mock_bot.pid = 12345
    mock_bot.restart_count = 0
    mock_bot.heartbeat_age_s = 5.0
    mock_bot.snapshot_lag_s = 10.0
    mock_bot.last_error = "Error: api_key=SUPER_SECRET_KEY_ABCDEF broke"
    mock_bot.spawned_at = _NOW

    supervisor = MagicMock()
    supervisor.status.return_value = [mock_bot]

    client = _make_client(supervisor=supervisor, secret_values=["SUPER_SECRET_KEY_ABCDEF"])
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "SUPER_SECRET_KEY_ABCDEF" not in body["bots"][0]["last_error"]
    assert "***REDACTED***" in body["bots"][0]["last_error"]


# ---------------------------------------------------------------------------
# /api/bots
# ---------------------------------------------------------------------------


def test_bots_list_shape() -> None:
    client = _make_client()
    resp = client.get("/api/bots")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    bot = body[0]
    assert bot["bot_id"] == "bot-001"
    assert bot["market_id"] == "market-abc"
    assert "state" in bot


def test_bots_list_etag_304() -> None:
    client = _make_client()
    resp1 = client.get("/api/bots")
    etag = resp1.headers["etag"]

    resp2 = client.get("/api/bots", headers={"if-none-match": etag})
    assert resp2.status_code == 304


# ---------------------------------------------------------------------------
# /api/bots/{id}
# ---------------------------------------------------------------------------


def test_bot_detail_shape() -> None:
    client = _make_client()
    resp = client.get("/api/bots/bot-001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bot_id"] == "bot-001"
    assert "recent_audit" in body
    assert isinstance(body["recent_audit"], list)


def test_bot_detail_404_when_not_found() -> None:
    db = _make_mock_db(bot_state=None)
    client = _make_client(db=db)
    resp = client.get("/api/bots/does-not-exist")
    assert resp.status_code == 404


def test_bot_detail_payload_redaction() -> None:
    secret = "MY_EXCHANGE_APIKEY_12345"
    audit = {
        "id": 1,
        "ts": _NOW,
        "bot_id": "bot-001",
        "kind": "order_submitted",
        "client_order_id": None,
        "exchange_order_id": None,
        "payload": {"note": f"used key {secret}"},
    }
    db = _make_mock_db(audit_rows=[audit])
    client = _make_client(db=db, secret_values=[secret])
    resp = client.get("/api/bots/bot-001")
    assert resp.status_code == 200
    body_str = json.dumps(resp.json())
    assert secret not in body_str
    assert "***REDACTED***" in body_str


# ---------------------------------------------------------------------------
# /api/strategies
# ---------------------------------------------------------------------------


def test_strategies_shape() -> None:
    client = _make_client()
    resp = client.get("/api/strategies")
    assert resp.status_code == 200
    body = resp.json()
    assert "strategies" in body
    s = body["strategies"][0]
    assert s["strategy"] == "momentum_v1"
    assert "wins" in s
    assert "gross_pnl" in s
    assert "n_bots" in s


def test_strategies_etag_304() -> None:
    client = _make_client()
    resp1 = client.get("/api/strategies")
    etag = resp1.headers["etag"]
    resp2 = client.get("/api/strategies", headers={"if-none-match": etag})
    assert resp2.status_code == 304


# ---------------------------------------------------------------------------
# /api/strategies/{name}
# ---------------------------------------------------------------------------


def test_strategy_detail_shape() -> None:
    client = _make_client()
    resp = client.get("/api/strategies/momentum_v1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy"] == "momentum_v1"
    assert "summary" in body
    assert "bots" in body
    assert isinstance(body["bots"], list)


def test_strategy_detail_404_when_not_found() -> None:
    db = _make_mock_db(strategy_detail_rows=[])
    client = _make_client(db=db)
    resp = client.get("/api/strategies/no_such_strategy")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/markets
# ---------------------------------------------------------------------------


def test_markets_shape() -> None:
    client = _make_client()
    resp = client.get("/api/markets")
    assert resp.status_code == 200
    body = resp.json()
    assert "markets" in body
    m = body["markets"][0]
    assert m["market_id"] == "market-abc"
    assert "gross_pnl" in m
    assert "n_bots" in m


# ---------------------------------------------------------------------------
# /api/audit
# ---------------------------------------------------------------------------


def test_audit_shape() -> None:
    client = _make_client()
    resp = client.get("/api/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    row = body[0]
    assert "id" in row
    assert "ts" in row
    assert "kind" in row
    assert "payload" in row


def test_audit_limit_capped_at_200() -> None:
    """Verify the endpoint caps limit at 200 regardless of query param."""
    db = _make_mock_db(audit_page_rows=[])
    client = _make_client(db=db)
    resp = client.get("/api/audit?limit=9999")
    assert resp.status_code == 200
    # Check the mock was called with limit=200
    call_kwargs = db.fetch_audit_page.call_args.kwargs  # type: ignore[union-attr]
    assert call_kwargs["limit"] == 200


def test_audit_etag_304() -> None:
    client = _make_client()
    resp1 = client.get("/api/audit")
    etag = resp1.headers["etag"]
    resp2 = client.get("/api/audit", headers={"if-none-match": etag})
    assert resp2.status_code == 304


# ---------------------------------------------------------------------------
# /api/failures
# ---------------------------------------------------------------------------


def test_failures_shape() -> None:
    client = _make_client()
    resp = client.get("/api/failures")
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "total" in body
    ev = body["events"][0]
    assert "ts" in ev
    assert "bot_id" in ev
    assert "kind" in ev


def test_failures_redacts_detail() -> None:
    secret = "superSECRET123456"
    failure = {
        "id": 99,
        "ts": _NOW,
        "bot_id": "bot-001",
        "kind": "order_rejected",
        "client_order_id": None,
        "exchange_order_id": None,
        "payload": {"reason": f"key={secret} invalid"},
    }
    db = _make_mock_db(failure_rows=[failure])
    client = _make_client(db=db, secret_values=[secret])
    resp = client.get("/api/failures")
    assert resp.status_code == 200
    body_str = json.dumps(resp.json())
    assert secret not in body_str


# ---------------------------------------------------------------------------
# /api/capital
# ---------------------------------------------------------------------------


def test_capital_shape_empty() -> None:
    db = _make_mock_db(capital_states=[])
    client = _make_client(db=db)
    resp = client.get("/api/capital")
    assert resp.status_code == 200
    body = resp.json()
    assert "total_usd" in body
    assert "per_exchange" in body
    assert body["total_usd"] == 0.0


def test_capital_aggregates_balances() -> None:
    states = [
        {
            "bot_id": "bot-001",
            "market_id": "market-abc",
            "snapshot_at": _NOW,
            "state": {"mode": "paper", "balance": 500.0, "exchange": "polymarket"},
        },
        {
            "bot_id": "bot-002",
            "market_id": "market-xyz",
            "snapshot_at": _NOW,
            "state": {"mode": "paper", "balance": 250.0, "exchange": "polymarket"},
        },
    ]
    db = _make_mock_db(capital_states=states)
    client = _make_client(db=db)
    resp = client.get("/api/capital")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_usd"] == pytest.approx(750.0)
    assert len(body["per_exchange"]) == 1
    assert body["per_exchange"][0]["exchange"] == "polymarket"
    assert body["per_exchange"][0]["balance"] == pytest.approx(750.0)


# ---------------------------------------------------------------------------
# ETag behavior — generic check
# ---------------------------------------------------------------------------


def test_cache_control_header_present() -> None:
    """Every endpoint should set Cache-Control."""
    client = _make_client()
    for path in ["/api/health", "/api/status", "/api/bots", "/api/strategies", "/api/markets"]:
        resp = client.get(path)
        assert "cache-control" in resp.headers, f"Missing Cache-Control on {path}"
