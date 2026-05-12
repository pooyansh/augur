"""Unit tests for the /metrics and /healthz routes added in Phase 6a.

Uses FastAPI's TestClient with minimal stubs — no Postgres, no real supervisor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app(health_checker: Any = None, supervisor: Any = None) -> FastAPI:
    """Build a minimal FastAPI app with the dashboard router."""
    from src.manager.dashboard.api import make_router
    from src.manager.dashboard.db import DashboardDb
    from src.manager.dashboard.redact import JsonRedactor

    db = MagicMock(spec=DashboardDb)
    db.ping = AsyncMock(return_value=True)
    db.max_snapshot_at = AsyncMock(return_value=None)
    db.fetch_all_bot_states = AsyncMock(return_value=[])

    redactor = JsonRedactor([])
    api_router, ops_router = make_router(
        db=db,
        redactor=redactor,
        supervisor=supervisor,
        health_checker=health_checker,
    )
    app = FastAPI()
    app.include_router(api_router)
    app.include_router(ops_router)
    return app


class _FakeHealthyChecker:
    async def check(self) -> dict[str, Any]:
        return {
            "healthy": True,
            "postgres_ok": True,
            "bots": [{"bot_id": "b1", "heartbeat_age_s": 5.0, "ok": True}],
            "ts": datetime.now(UTC).isoformat(),
        }


class _FakeUnhealthyChecker:
    async def check(self) -> dict[str, Any]:
        return {
            "healthy": False,
            "postgres_ok": False,
            "bots": [{"bot_id": "b1", "heartbeat_age_s": 120.0, "ok": False}],
            "ts": datetime.now(UTC).isoformat(),
        }


def test_metrics_endpoint_returns_prometheus_text() -> None:
    """GET /metrics returns Prometheus text format with expected metric names."""
    # Ensure at least one metric has been observed so the output is non-empty.
    from src.observability.metrics import BOT_HEARTBEAT_AGE_SECONDS

    BOT_HEARTBEAT_AGE_SECONDS.labels(bot_id="test-metrics-route").set(42.0)

    app = _make_app()
    client = TestClient(app)
    response = client.get("/metrics")

    assert response.status_code == 200
    body = response.text
    # Should contain at least the HELP/TYPE lines for our custom metrics.
    assert "bot_heartbeat_age_seconds" in body or "bot_tick_latency" in body


def test_healthz_returns_200_when_healthy() -> None:
    """GET /healthz returns 200 with healthy=true when checker reports healthy."""
    app = _make_app(health_checker=_FakeHealthyChecker())
    client = TestClient(app)
    response = client.get("/healthz")

    assert response.status_code == 200
    data = response.json()
    assert data["healthy"] is True
    assert data["postgres_ok"] is True
    assert len(data["bots"]) == 1
    assert data["bots"][0]["ok"] is True


def test_healthz_returns_503_when_unhealthy() -> None:
    """GET /healthz returns 503 with diagnostic body when checker reports unhealthy."""
    app = _make_app(health_checker=_FakeUnhealthyChecker())
    client = TestClient(app)
    response = client.get("/healthz")

    assert response.status_code == 503
    data = response.json()
    assert data["healthy"] is False
    assert data["postgres_ok"] is False
    assert len(data["bots"]) == 1
    assert data["bots"][0]["ok"] is False
    assert data["bots"][0]["heartbeat_age_s"] == 120.0


def test_healthz_no_checker_returns_200() -> None:
    """GET /healthz with no health_checker wired returns 200 (minimal healthy)."""
    app = _make_app(health_checker=None)
    client = TestClient(app)
    response = client.get("/healthz")

    assert response.status_code == 200
    data = response.json()
    assert data["healthy"] is True
