"""One-window dip-buyer for Polymarket BTC Up/Down 15m markets.

Watches the live CLOB WebSocket for both UP and DOWN outcomes via
:class:`~src.feeds.clob_feed.ClobPriceFeed`.  When either side's ask drops
to or below the trigger price (default 0.30), it places a single BUY order
and exits.

One trade per run. Does NOT auto-follow to the next window.

Usage:
    SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt \\
        uv run python scripts/snipe_bot.py --bot-id btc-updown-paper-1

Options:
    --trigger  Ask price at or below which to fire (default 0.30)
    --size     Shares to buy (default 5, Polymarket minimum)

Required env:
    SOPS_AGE_KEY_FILE  path to age private key (~/.config/sops/age/keys.txt)
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
import time
from datetime import datetime
from decimal import ROUND_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import click
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

_ET = ZoneInfo("America/New_York")


def _ts() -> str:
    return datetime.now(tz=_ET).strftime("%H:%M:%S")


def _load_secrets() -> dict:
    enc = ROOT / "secrets" / "exchanges.enc.yaml"
    r = subprocess.run(["sops", "--decrypt", str(enc)], capture_output=True, text=True)
    if r.returncode != 0:
        click.echo(f"ERROR decrypting secrets:\n{r.stderr}", err=True)
        sys.exit(1)
    return yaml.safe_load(r.stdout)


def _effective_size(size: Decimal, ask: Decimal) -> Decimal:
    """Bump size up if needed to meet the $1.00 CLOB minimum notional.

    Args:
        size: Requested share count.
        ask: Current best ask price.

    Returns:
        Adjusted share count that satisfies the $1 notional minimum.
    """
    min_notional = Decimal("1.00")
    if ask * size < min_notional:
        bumped = (min_notional / ask).to_integral_value(rounding=ROUND_UP)
        click.echo(f"[{_ts()}] Size {size} -> {bumped} to meet $1 min notional")
        return bumped
    return size


async def _buy(
    outcome: str,
    token_id: str,
    ask_price: Decimal,
    size: Decimal,
    activity: object,
) -> None:
    """Place a live BUY order using the Polymarket adapter.

    Args:
        outcome: Human-readable outcome name (e.g. ``"Up"``).
        token_id: ERC-1155 token ID for the outcome.
        ask_price: Limit price to use for the order.
        size: Number of shares to buy.
        activity: :class:`~src.infra.activity_logger.ActivityLogger` instance.
    """
    from src.exchanges.base import Market, Mode, OrderIntent, Side
    from src.exchanges.polymarket import PolymarketAdapter, load_polymarket_config
    from src.risk.audit import KIND_ORDER_INTENT, KIND_ORDER_RESULT

    secrets = _load_secrets()
    config = load_polymarket_config(secrets.get("polymarket", secrets))

    market = Market(
        market_id="",
        token_id=token_id,
        tick_size=Decimal("0.01"),
        min_size=Decimal("5"),
        venue="polymarket",
    )

    coid = hashlib.blake2s(
        f"snipe:{token_id}:buy:{ask_price}:{size}".encode(), digest_size=8
    ).hexdigest()

    intent = OrderIntent(
        client_order_id=coid,
        market=market,
        side=Side.BUY,
        price=ask_price,
        size=size,
    )

    click.echo(f"[{_ts()}] FIRING -- BUY {size} {outcome} @ {ask_price}  (coid {coid})")
    await activity.log(  # type: ignore[attr-defined]
        KIND_ORDER_INTENT,
        {
            "outcome": outcome,
            "token_id": token_id,
            "price": str(ask_price),
            "size": str(size),
        },
        client_order_id=coid,
    )

    async with PolymarketAdapter(Mode.LIVE, config) as adapter:
        result = await adapter.place(intent)

    if result.accepted:
        click.echo(f"[{_ts()}] Order accepted -- exchange id: {result.exchange_order_id}")
        await activity.log(  # type: ignore[attr-defined]
            KIND_ORDER_RESULT,
            {"accepted": True, "exchange_order_id": result.exchange_order_id},
            client_order_id=coid,
            exchange_order_id=result.exchange_order_id,
        )
    else:
        click.echo(f"[{_ts()}] Order rejected -- {result.reason}", err=True)
        click.echo(f"  raw: {result.raw}", err=True)
        await activity.log(  # type: ignore[attr-defined]
            KIND_ORDER_RESULT,
            {"accepted": False, "reason": result.reason},
            client_order_id=coid,
        )


async def _watch_and_snipe(bot_id: str, trigger: Decimal, size: Decimal) -> None:
    from src.exchanges.market_resolver import resolve_all_outcomes
    from src.feeds.clob_feed import ClobPriceFeed
    from src.infra.activity_logger import ActivityLogger
    from src.manager.config import load_roster
    from src.risk.audit import (
        KIND_BOT_STARTED,
        KIND_BOT_STOPPED,
        KIND_ERROR,
        KIND_MARKET_RESOLVED,
        KIND_TRIGGER_FIRED,
        KIND_TRIGGER_MISSED,
    )

    async with ActivityLogger.create(bot_id) as activity:
        await activity.log(
            KIND_BOT_STARTED,
            {"script": "snipe_bot", "trigger": str(trigger), "size": str(size)},
        )

        click.echo(f"\nResolving current window for {bot_id!r}...")
        roster = load_roster(ROOT / "config" / "bots.yaml")
        entry = next((b for b in roster.bots if b.id == bot_id), None)
        if entry is None:
            click.echo(f"ERROR: bot {bot_id!r} not found.", err=True)
            sys.exit(1)
        if not entry.market.slug:
            click.echo("ERROR: bot must use a slug-based market.", err=True)
            sys.exit(1)

        try:
            resolved = resolve_all_outcomes(entry.market)
        except Exception as exc:
            await activity.log(KIND_ERROR, {"error": str(exc), "phase": "market_resolve"})
            raise

        tokens = resolved.tokens
        window_end = resolved.window_end or (time.time() + 900)

        await activity.log(
            KIND_MARKET_RESOLVED,
            {
                "condition_id": resolved.condition_id,
                "slug": resolved.slug,
                "outcomes": list(tokens.keys()),
                "window_end": window_end,
            },
        )

        # Map outcome names case-insensitively to canonical names
        outcome_map = {k.lower(): (k, v) for k, v in tokens.items()}
        up_label, up_token = outcome_map.get("up", outcome_map.get("yes", ("Up", "")))
        dn_label, dn_token = outcome_map.get("down", outcome_map.get("no", ("Down", "")))

        if not up_token or not dn_token:
            click.echo(f"ERROR: unexpected outcomes {list(tokens.keys())}")
            await activity.log(
                KIND_ERROR,
                {"error": f"unexpected outcomes: {list(tokens.keys())}"},
            )
            sys.exit(1)

        secs_left = int(window_end - time.time())
        click.echo(f"  UP   token: ...{up_token[-8:]}")
        click.echo(f"  DOWN token: ...{dn_token[-8:]}")
        click.echo(f"  Window closes in: {secs_left // 60}m {secs_left % 60:02d}s")
        click.echo(f"  Trigger: ask <= {trigger}  |  Size: {size} shares")
        click.echo(f"\n{'─' * 70}")
        click.echo("  Watching via ClobPriceFeed... (Ctrl-C to abort)")
        click.echo(f"{'─' * 70}\n")

        ask_of: dict[str, Decimal] = {
            up_token: Decimal("1"),
            dn_token: Decimal("1"),
        }
        side_of: dict[str, str] = {up_token: up_label, dn_token: dn_label}
        fired = False

        try:
            async with ClobPriceFeed(tokens) as feed:
                async for update in feed:
                    if fired or time.time() >= window_end:
                        break

                    if update.token_id not in ask_of:
                        continue

                    old_ask = ask_of[update.token_id]
                    ask_of[update.token_id] = update.ask

                    up_ask = ask_of[up_token]
                    dn_ask = ask_of[dn_token]
                    moved = (
                        f"  <- {side_of[update.token_id]} {old_ask:.2f}->{update.ask:.2f}"
                        if update.ask != old_ask
                        else ""
                    )
                    click.echo(f"[{_ts()}]  UP ask={up_ask:.2f}  DOWN ask={dn_ask:.2f}{moved}")

                    # Check trigger on both tokens
                    for t_id, outcome_name in side_of.items():
                        ask = ask_of[t_id]
                        if ask > Decimal("0") and ask <= trigger:
                            fired = True
                            effective = _effective_size(size, ask)
                            click.echo(
                                f"\n[{_ts()}] TRIGGER -- {outcome_name} ask={ask:.2f} <= {trigger}"
                            )
                            await activity.log(
                                KIND_TRIGGER_FIRED,
                                {
                                    "outcome": outcome_name,
                                    "ask": str(ask),
                                    "trigger": str(trigger),
                                    "size": str(effective),
                                    "source": update.source,
                                },
                            )
                            await _buy(outcome_name, t_id, ask, effective, activity)
                            click.echo(f"\n[{_ts()}] Done. Holding until window closes.")
                            # Wait out the rest of the window
                            remaining = max(0.0, window_end - time.time())
                            await asyncio.sleep(remaining)
                            break

        except asyncio.CancelledError:
            await activity.log(KIND_BOT_STOPPED, {"reason": "cancelled", "fired": fired})
            raise
        except KeyboardInterrupt:
            await activity.log(KIND_BOT_STOPPED, {"reason": "keyboard_interrupt", "fired": fired})
            raise
        except Exception as exc:
            await activity.log(KIND_ERROR, {"error": str(exc), "fired": fired})
            raise

        if not fired:
            click.echo(f"\n[{_ts()}] Window closed -- no trigger fired. Exiting.")
            await activity.log(
                KIND_TRIGGER_MISSED,
                {
                    "trigger": str(trigger),
                    "final_up_ask": str(ask_of.get(up_token, Decimal("1"))),
                    "final_dn_ask": str(ask_of.get(dn_token, Decimal("1"))),
                },
            )

        await activity.log(KIND_BOT_STOPPED, {"reason": "window_expired", "fired": fired})


@click.command()
@click.option("--bot-id", required=True, help="Bot ID from config/bots.yaml")
@click.option(
    "--trigger",
    default="0.30",
    show_default=True,
    help="Fire when ask drops to or below this price",
)
@click.option("--size", default="5", show_default=True, help="Shares to buy")
def main(bot_id: str, trigger: str, size: str) -> None:
    """Watch UP+DOWN asks. Buy shares of the first side that hits the trigger."""
    try:
        asyncio.run(_watch_and_snipe(bot_id, Decimal(trigger), Decimal(size)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        click.echo("\nAborted.")


if __name__ == "__main__":
    main()
