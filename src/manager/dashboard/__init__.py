"""Operator dashboard — read-only HTTP API + static SPA server.

Exposes a FastAPI router on ``127.0.0.1:8090`` (configurable) inside the
manager's asyncio loop.  The SPA (``web/dist/``) is served from the same
origin.

Public re-exports:
    DashboardServer — lifecycle wrapper (start/stop).
    PerfRollupRefresher — background task that refreshes ``perf_rollup`` every 60 s.
    DashboardDb — read-only asyncpg pool with typed query helpers.
"""

from __future__ import annotations

__all__ = [
    "DashboardDb",
    "DashboardServer",
    "PerfRollupRefresher",
]

from src.manager.dashboard.db import DashboardDb
from src.manager.dashboard.rollup import PerfRollupRefresher
from src.manager.dashboard.server import DashboardServer
