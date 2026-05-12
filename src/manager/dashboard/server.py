"""DashboardServer — builds the FastAPI app and manages the uvicorn lifecycle.

Mounts:
- The API router at ``/api``
- The static SPA bundle (``web/dist/``) at ``/`` via ``StaticFiles``

The server is bound to ``127.0.0.1`` only in v1.  Public exposure is a
Phase 9 decision; see ``api.py`` for the TODO marker.

Lifecycle:
    server = DashboardServer(db=db, redactor=redactor, supervisor=supervisor)
    await server.start(host="127.0.0.1", port=8090)
    # ... manager runs ...
    await server.stop()
"""

from __future__ import annotations

__all__ = ["DashboardServer"]

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.manager.dashboard.api import make_router
from src.manager.dashboard.db import DashboardDb
from src.manager.dashboard.redact import JsonRedactor

logger = logging.getLogger(__name__)

# Path to the compiled SPA bundle relative to the project root.
_WEB_DIST = Path(__file__).parents[4] / "web" / "dist"


class DashboardServer:
    """Wraps a FastAPI app + uvicorn server with async start/stop lifecycle.

    Args:
        db: Opened DashboardDb instance.
        redactor: JsonRedactor configured with current secret values.
        supervisor: Optional Supervisor for /api/status and /api/health.
    """

    def __init__(
        self,
        db: DashboardDb,
        redactor: JsonRedactor,
        supervisor: Any | None = None,
    ) -> None:
        self._db = db
        self._redactor = redactor
        self._supervisor = supervisor
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    def _build_app(self) -> FastAPI:
        """Build the FastAPI application.

        Returns:
            Configured FastAPI instance with API router and static SPA mounted.
        """
        app = FastAPI(
            title="PolymarketBidderBot Dashboard",
            description="Read-only operator dashboard.",
            version="1.0.0",
            # Phase 9: add SecurityScheme here for bearer-token auth.
            docs_url="/api/docs",
            redoc_url="/api/redoc",
        )

        router = make_router(
            db=self._db,
            redactor=self._redactor,
            supervisor=self._supervisor,
        )
        app.include_router(router)

        # Mount the SPA bundle if it exists.
        if _WEB_DIST.is_dir():
            app.mount(
                "/",
                StaticFiles(directory=str(_WEB_DIST), html=True),
                name="spa",
            )
            logger.info("SPA bundle mounted from %s", _WEB_DIST)
        else:
            logger.warning(
                "web/dist/ not found at %s — SPA not served. "
                "Run `cd web && npm run build` to build the bundle.",
                _WEB_DIST,
            )

        return app

    async def start(self, host: str = "127.0.0.1", port: int = 8090) -> None:
        """Start the uvicorn server inside the current asyncio event loop.

        Args:
            host: Bind address (must be 127.0.0.1 in v1).
            port: TCP port to listen on.
        """
        app = self._build_app()
        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            loop="asyncio",
            log_level="warning",  # Uvicorn's own logs go to warning; app logs handled by manager.
            access_log=False,  # Reduce noise; operator accesses via browser.
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(
            self._server.serve(),
            name="dashboard-uvicorn",
        )
        logger.info("Dashboard server starting on http://%s:%d/", host, port)

    async def stop(self) -> None:
        """Gracefully shut down the uvicorn server."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except (asyncio.CancelledError, TimeoutError):
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
        logger.info("Dashboard server stopped.")
