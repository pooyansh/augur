"""Stream live order-book updates for a Polymarket market to stdout.

Subscribes to BOTH outcomes (UP + DOWN) simultaneously so you can see
the full market. Shows bid/ask for each side plus the implied buy/sell
prices. Auto-follows to the next 15-minute window when the current one closes.

Usage:
    uv run python scripts/watch_market.py --bot-id btc-updown-paper-1

Timestamps are US/Eastern. No credentials required — public WS feed.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import click

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

_ET = ZoneInfo("America/New_York")
_WINDOW = 900  # seconds per recurring window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> str:
    return datetime.now(tz=_ET).strftime("%H:%M:%S.%f")[:-3]


def _countdown(window_end: float) -> str:
    secs = max(0, int(window_end - time.time()))
    return f"{secs // 60}m {secs % 60:02d}s"


def _book_line(
    label: str,
    window_end: float,
    outcome_state: dict[str, tuple[Decimal, Decimal]],
    event: str = "BOOK",
) -> str:
    parts = []
    for outcome, (bid, ask) in outcome_state.items():
        parts.append(f"{outcome}  bid={bid:.2f} ask={ask:.2f} buy@{ask:.2f} sell@{bid:.2f}")
    mid = "  |  ".join(parts)
    return f"[{_ts()}] {event:<5}  {mid}  |  closes {_countdown(window_end)}"


# ---------------------------------------------------------------------------
# Main stream loop
# ---------------------------------------------------------------------------


async def _stream(bot_id: str) -> None:
    import structlog
    from src.exchanges.market_resolver import resolve_all_outcomes
    from src.feeds.clob_feed import ClobPriceFeed
    from src.infra.activity_logger import ActivityLogger
    from src.manager.config import load_roster
    from src.risk.audit import (
        KIND_BOT_STARTED,
        KIND_BOT_STOPPED,
        KIND_FEED_STARTED,
        KIND_MARKET_RESOLVED,
    )

    log = structlog.get_logger()

    async with ActivityLogger.create(bot_id) as activity:
        await activity.log(KIND_BOT_STARTED, {"script": "watch_market"})

        # Resolve market from bot config
        click.echo(f"\nResolving market for bot {bot_id!r}...")
        roster = load_roster(ROOT / "config" / "bots.yaml")
        entry = next((b for b in roster.bots if b.id == bot_id), None)
        if entry is None:
            ids = [b.id for b in roster.bots]
            click.echo(f"ERROR: bot {bot_id!r} not found. Available: {ids}", err=True)
            sys.exit(1)
        if entry.market.exchange != "polymarket":
            click.echo(
                f"ERROR: bot {bot_id!r} uses exchange {entry.market.exchange!r}; "
                "only polymarket is supported.",
                err=True,
            )
            sys.exit(1)
        if entry.market.slug is None:
            click.echo(
                "ERROR: static markets require --token-id; use a slug-based bot.",
                err=True,
            )
            sys.exit(1)

        resolved = resolve_all_outcomes(entry.market)
        window_end = resolved.window_end or (time.time() + _WINDOW)
        tokens = resolved.tokens

        await activity.log(
            KIND_MARKET_RESOLVED,
            {
                "condition_id": resolved.condition_id,
                "slug": resolved.slug,
                "outcomes": list(tokens.keys()),
                "window_end": window_end,
            },
        )

        click.echo(f"  slug         : {resolved.slug}")
        click.echo(f"  condition_id : {resolved.condition_id}")
        click.echo(f"  outcomes     : {list(tokens.keys())}")
        click.echo(f"  window ends  : {_countdown(window_end)}")

        # Track current bid/ask per outcome
        outcome_state: dict[str, tuple[Decimal, Decimal]] = {
            outcome: (Decimal("0"), Decimal("1")) for outcome in tokens
        }

        click.echo(f"\n{'─' * 110}")
        click.echo(
            f"  {resolved.slug}  "
            + "  |  ".join(f"{k}: bid=0.00 ask=1.00" for k in tokens)
            + f"  |  closes in {_countdown(window_end)} ET"
        )
        click.echo(f"{'─' * 110}\n")

        while True:
            # Window expiry — re-resolve and restart
            if time.time() >= window_end:
                click.echo(f"\n{'=' * 110}")
                click.echo(f"  Window closed — resolving next window for {entry.market.slug} ...")
                click.echo(f"{'=' * 110}\n")
                resolved = resolve_all_outcomes(entry.market)
                window_end = resolved.window_end or (time.time() + _WINDOW)
                tokens = resolved.tokens
                outcome_state = {outcome: (Decimal("0"), Decimal("1")) for outcome in tokens}

                await activity.log(
                    KIND_MARKET_RESOLVED,
                    {
                        "condition_id": resolved.condition_id,
                        "slug": resolved.slug,
                        "outcomes": list(tokens.keys()),
                        "window_end": window_end,
                    },
                )

            await activity.log(
                KIND_FEED_STARTED,
                {"slug": resolved.slug, "token_count": len(tokens)},
            )

            try:
                async with ClobPriceFeed(tokens) as feed:
                    async for update in feed:
                        # Window expiry check on every event
                        if time.time() >= window_end:
                            break

                        outcome_state[update.outcome] = (update.bid, update.ask)

                        event_label = {
                            "ws_snapshot": "SNAP",
                            "ws_price_change": "BOOK",
                            "rest_poll": "POLL",
                        }.get(update.source, "UPD")

                        click.echo(
                            _book_line(resolved.slug, window_end, outcome_state, event_label)
                        )

                        # Debug-level structured log (stdout only, not to DB)
                        log.debug(
                            "price_update",
                            token_id=update.token_id,
                            outcome=update.outcome,
                            bid=str(update.bid),
                            ask=str(update.ask),
                            source=update.source,
                        )

            except asyncio.CancelledError:
                await activity.log(KIND_BOT_STOPPED, {"reason": "cancelled"})
                raise
            except KeyboardInterrupt:
                await activity.log(KIND_BOT_STOPPED, {"reason": "keyboard_interrupt"})
                raise
            except Exception as exc:
                click.echo(f"[{_ts()}] feed error — {exc}  (restarting...)", err=True)
                log.warning("feed_error", error=str(exc))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.option("--bot-id", required=True, help="Bot ID from config/bots.yaml")
def main(bot_id: str) -> None:
    """Stream live UP+DOWN prices for a Polymarket market. Ctrl-C to quit."""
    try:
        asyncio.run(_stream(bot_id))
    except (KeyboardInterrupt, asyncio.CancelledError):
        click.echo("\nStopped.")


if __name__ == "__main__":
    main()
