"""inspect-state implementation — pretty-prints the latest bot snapshot.

Invoked as::

    uv run python -m src.manager inspect-state <bot_id>

Connects to Postgres via ``POSTGRES_*`` environment variables, fetches the
latest ``bot_state`` row, and prints it as indented JSON.  If the bot is not
found, it lists all known bot ids.
"""

from __future__ import annotations

__all__ = ["run_inspect"]

import json
import logging
import os
import sys

logger = logging.getLogger(__name__)


async def run_inspect(bot_id: str) -> None:
    """Fetch and print the latest snapshot for ``bot_id``.

    Args:
        bot_id: The bot to inspect.
    """
    host = os.environ.get("POSTGRES_HOST")
    if host:
        await _inspect_from_postgres(bot_id)
    else:
        print(
            "POSTGRES_HOST is not set. Cannot connect to the database.\n"
            "Set POSTGRES_HOST, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB "
            "to inspect a live snapshot.",
            file=sys.stderr,
        )
        sys.exit(1)


async def _inspect_from_postgres(bot_id: str) -> None:
    """Connect to Postgres and pretty-print the latest snapshot for ``bot_id``.

    Args:
        bot_id: Bot identifier to look up.
    """
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "bidder")
    user = os.environ.get("POSTGRES_USER", "bidder")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ["POSTGRES_HOST"]

    url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"

    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.state.models import BotState

    engine = create_async_engine(url, pool_pre_ping=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    async with sf() as session:
        # Try to fetch the specific bot.
        result = await session.execute(
            select(BotState)
            .where(BotState.bot_id == bot_id)
            .order_by(BotState.snapshot_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()

        if row is None:
            # List all known bot ids.
            all_ids_result = await session.execute(
                text("SELECT DISTINCT bot_id FROM bot_state ORDER BY bot_id")
            )
            known_ids = [r[0] for r in all_ids_result]
            if known_ids:
                print(
                    f"Bot '{bot_id}' not found in bot_state.\n"
                    f"Known bot ids:\n" + "\n".join(f"  - {bid}" for bid in known_ids),
                    file=sys.stderr,
                )
            else:
                print(
                    f"Bot '{bot_id}' not found. The bot_state table is empty.",
                    file=sys.stderr,
                )
            await engine.dispose()
            sys.exit(1)

        output = {
            "bot_id": row.bot_id,
            "market_id": row.market_id,
            "snapshot_at": row.snapshot_at.isoformat() if row.snapshot_at else None,
            "version": row.version,
            "state": row.state,
        }
        print(json.dumps(output, indent=2, default=str))

    await engine.dispose()
