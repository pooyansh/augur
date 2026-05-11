"""Bot subprocess entrypoint.

Launched by the supervisor as::

    uv run python -m src.bots.runner --bot-id <id>

Steps:
1. Configure logging with secret redaction.
2. Install SIGTERM handler for graceful shutdown.
3. Load ``config/bots.yaml`` and locate the entry by id.
4. Honor ``BOT_MODE_OVERRIDE`` env (live-downgrade path from the supervisor).
5. Resolve strategy class via ``StrategyRegistry.autodiscover()``.
6. Build ``BotDeps``: real infrastructure or in-memory stubs based on env.
7. Build the exchange adapter via :func:`~src.exchanges.echo.make_adapter`.
8. Construct the strategy, call ``rehydrate`` if a snapshot exists, ``await bot.run()``.
9. On SIGTERM: persist a final snapshot, close the heartbeat socket, exit 0.
"""

from __future__ import annotations

__all__ = ["main"]

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import click

logger = logging.getLogger(__name__)


def _build_session_factory() -> object:
    """Build a SQLAlchemy async session factory from environment variables.

    Returns:
        An ``async_sessionmaker`` bound to the configured Postgres URL, or
        ``None`` if Postgres variables are absent (unit test / offline path).
    """
    host = os.environ.get("POSTGRES_HOST")
    if not host:
        return None

    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "bidder")
    user = os.environ.get("POSTGRES_USER", "bidder")
    password = os.environ.get("POSTGRES_PASSWORD", "")

    url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _run(bot_id: str) -> None:
    """Async bot runner — the main async entrypoint.

    Args:
        bot_id: Stable bot identifier to look up in the roster.
    """
    from decimal import Decimal

    from src.bots.base import BotConfig, BotDeps, Clock, RiskCaps, Schedule
    from src.exchanges.base import Mode
    from src.exchanges.echo import make_adapter
    from src.manager.config import load_roster
    from src.manager.heartbeat import HeartbeatClient, SocketHeartbeat
    from src.manager.registry import registry
    from src.secrets.install import configure_logging_with_redaction
    from src.secrets.loader import load_secrets

    # ------------------------------------------------------------------
    # 1. Logging + redaction
    # ------------------------------------------------------------------
    secrets_dir = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))
    loaded_secrets = load_secrets(secrets_dir)
    configure_logging_with_redaction(loaded_secrets)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # ------------------------------------------------------------------
    # 3. Load roster and find the bot entry
    # ------------------------------------------------------------------
    config_path = Path(os.environ.get("BOTS_CONFIG", "config/bots.yaml"))
    try:
        roster = load_roster(config_path)
    except Exception as exc:
        logger.critical("Failed to load roster from %s: %s", config_path, exc)
        sys.exit(2)

    entry = next((e for e in roster.bots if e.id == bot_id), None)
    if entry is None:
        known = [e.id for e in roster.bots]
        logger.critical("Bot id %r not found in roster. Known ids: %s", bot_id, known)
        sys.exit(2)

    # ------------------------------------------------------------------
    # 4. Honor BOT_MODE_OVERRIDE (supervisor fail-closed downgrade)
    # ------------------------------------------------------------------
    mode_str = os.environ.get("BOT_MODE_OVERRIDE", entry.mode)
    mode = Mode(mode_str)

    # ------------------------------------------------------------------
    # 5. Auto-discover strategy registry
    # ------------------------------------------------------------------
    registry.autodiscover()
    try:
        strategy_class = registry.get(entry.strategy)
    except KeyError as exc:
        logger.critical("Strategy not found: %s", exc)
        sys.exit(2)

    # ------------------------------------------------------------------
    # 6. Build BotDeps
    # ------------------------------------------------------------------
    clock = Clock()
    session_factory = _build_session_factory()

    if session_factory is not None:
        from src.state.repository import AuditLogger, KillSwitchReader, StateRepository

        state = StateRepository(session_factory)  # type: ignore[arg-type]
        kill_switch = KillSwitchReader(session_factory)  # type: ignore[arg-type]
        audit = AuditLogger(session_factory)  # type: ignore[arg-type]
    else:
        # Offline / test path — use in-memory stubs.
        logger.warning("POSTGRES_HOST not set — using in-memory state stubs (no persistence).")
        from tests.fixtures.state import (
            InMemoryAuditLogger,
            InMemoryKillSwitch,
            InMemoryStateRepository,
        )

        state = InMemoryStateRepository()  # type: ignore[assignment]
        kill_switch = InMemoryKillSwitch()  # type: ignore[assignment]
        audit = InMemoryAuditLogger()  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Heartbeat client
    # ------------------------------------------------------------------
    sock_dir = Path(os.environ.get("MANAGER_SOCKET_DIR", "/run/manager"))
    sock_path = sock_dir / f"{bot_id}.sock"
    hb_client = HeartbeatClient(sock_path=sock_path, bot_id=bot_id, clock=clock)
    heartbeat = SocketHeartbeat(hb_client)

    # ------------------------------------------------------------------
    # 7. Build adapter
    # ------------------------------------------------------------------
    adapter = make_adapter(entry.market.exchange, mode)

    # ------------------------------------------------------------------
    # Build BotConfig
    # ------------------------------------------------------------------
    risk_override = entry.risk
    bot_config = BotConfig(
        bot_id=entry.id,
        strategy_name=entry.strategy,
        market_id=entry.market.market_id,
        mode=mode,
        live=(mode == Mode.LIVE),
        schedule=Schedule(every_seconds=entry.schedule_seconds()),
        risk=RiskCaps(
            max_position_notional=Decimal(str(risk_override.max_position_notional)),
            max_daily_loss=Decimal(str(risk_override.max_daily_loss)),
            max_orders_per_minute=risk_override.max_orders_per_minute,
        ),
        signal_subscriptions=[s.name for s in entry.signals],
    )

    # Build Market from the adapter.

    market = await adapter.get_market(entry.market.market_id)

    deps = BotDeps(
        adapter=adapter,
        state=state,
        kill_switch=kill_switch,
        heartbeat=heartbeat,
        audit=audit,
        clock=clock,
    )

    # ------------------------------------------------------------------
    # 8. Construct strategy, rehydrate if snapshot exists
    # ------------------------------------------------------------------
    bot = strategy_class(market=market, config=bot_config, deps=deps)

    latest = await state.latest_snapshot(bot_id)
    if latest is not None:
        ver = latest.get("_version")
        logger.info("Rehydrating bot %s from snapshot (version=%s).", bot_id, ver)
        bot.rehydrate(latest)
    else:
        logger.info("No snapshot found for bot %s — starting fresh.", bot_id)

    # ------------------------------------------------------------------
    # SIGTERM handler — write final snapshot then exit
    # ------------------------------------------------------------------
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _on_sigterm() -> None:
        logger.info("Bot %s received SIGTERM — initiating graceful shutdown.", bot_id)
        shutdown_event.set()

    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)

    # ------------------------------------------------------------------
    # Run the bot
    # ------------------------------------------------------------------
    run_task = asyncio.create_task(bot.run(), name=f"bot-run-{bot_id}")

    # Wait for either the bot to finish or SIGTERM.
    import contextlib

    _done, _pending = await asyncio.wait(
        [run_task, asyncio.create_task(shutdown_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel the run task if shutdown was signalled.
    if not run_task.done():
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task

    # 9. Final snapshot on shutdown
    try:
        await bot._persist_snapshot()
        logger.info("Final snapshot written for bot %s.", bot_id)
    except Exception as exc:
        logger.warning("Final snapshot write failed for bot %s: %s", bot_id, exc)

    # Close heartbeat
    await hb_client.close()
    logger.info("Bot %s shutdown complete.", bot_id)


@click.command()
@click.option("--bot-id", required=True, help="Stable bot identifier from config/bots.yaml.")
def main(bot_id: str) -> None:
    """Run a single bot subprocess.

    Reads config/bots.yaml, resolves the strategy, wires dependencies, and
    runs the bot's async loop until SIGTERM.

    Args:
        bot_id: Stable bot identifier matching an entry in the roster.
    """
    asyncio.run(_run(bot_id))


if __name__ == "__main__":
    main()
