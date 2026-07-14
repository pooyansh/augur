"""Unit tests for the dashboard control router.

Mirrors the style of ``tests/unit/test_dashboard_api.py``: FastAPI's
``TestClient`` against a router built from mocked dependencies — no real
Supervisor, no real Postgres.

Covers:
- The happy path: a running bot is stopped, response shape is correct, and
  an audit row is written.
- The not-running path: ``stopped=False``, still 200, still audit-logged.
- The "stays singular" invariant: ``control_router`` has exactly one route
  and it is a POST — guards against this write-like surface quietly growing
  (see `.claude/rules/06b-dashboard.md` invariant 1a).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.manager.dashboard.control_api import make_control_router
from src.risk.audit import KIND_BOT_STOP_REQUESTED


def _make_client(supervisor: Any, audit: Any) -> TestClient:
    """Build a TestClient mounting only the control router."""
    router = make_control_router(supervisor=supervisor, audit=audit)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _make_supervisor(stop_bot_result: bool) -> Any:
    supervisor = MagicMock()
    supervisor.stop_bot = AsyncMock(return_value=stop_bot_result)
    return supervisor


def _make_audit() -> Any:
    audit = MagicMock()
    audit.write = AsyncMock()
    return audit


# ---------------------------------------------------------------------------
# POST /api/control/bots/{id}/stop
# ---------------------------------------------------------------------------


def test_stop_bot_running_returns_stopped_true() -> None:
    supervisor = _make_supervisor(stop_bot_result=True)
    audit = _make_audit()
    client = _make_client(supervisor, audit)

    resp = client.post("/api/control/bots/bot-001/stop")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"bot_id": "bot-001", "stopped": True}
    supervisor.stop_bot.assert_awaited_once_with("bot-001")


def test_stop_bot_not_running_returns_stopped_false() -> None:
    supervisor = _make_supervisor(stop_bot_result=False)
    audit = _make_audit()
    client = _make_client(supervisor, audit)

    resp = client.post("/api/control/bots/bot-ghost/stop")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"bot_id": "bot-ghost", "stopped": False}


def test_stop_bot_writes_audit_row() -> None:
    supervisor = _make_supervisor(stop_bot_result=True)
    audit = _make_audit()
    client = _make_client(supervisor, audit)

    client.post("/api/control/bots/bot-001/stop")

    audit.write.assert_awaited_once()
    kwargs = audit.write.call_args.kwargs
    assert kwargs["bot_id"] == "bot-001"
    assert kwargs["kind"] == KIND_BOT_STOP_REQUESTED
    assert kwargs["payload"]["source"] == "dashboard"
    assert kwargs["payload"]["result"] == "stopped"


def test_stop_bot_not_running_audit_result_is_not_running() -> None:
    supervisor = _make_supervisor(stop_bot_result=False)
    audit = _make_audit()
    client = _make_client(supervisor, audit)

    client.post("/api/control/bots/bot-ghost/stop")

    kwargs = audit.write.call_args.kwargs
    assert kwargs["payload"]["result"] == "not_running"


# ---------------------------------------------------------------------------
# Invariant: exactly one route, and it's a POST.
# ---------------------------------------------------------------------------


def test_control_router_has_exactly_one_route_and_it_is_post() -> None:
    router = make_control_router(supervisor=_make_supervisor(True), audit=_make_audit())

    assert len(router.routes) == 1
    (route,) = router.routes
    assert route.methods == {"POST"}  # type: ignore[attr-defined]
    assert route.path == "/api/control/bots/{bot_id}/stop"  # type: ignore[attr-defined]
