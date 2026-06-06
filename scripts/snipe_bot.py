"""One-window dip-buyer for Polymarket BTC Up/Down recurring markets.

Watches the live CLOB WebSocket for both UP and DOWN outcomes via
:class:`~src.feeds.clob_feed.ClobPriceFeed`.  When either side's ask drops
to or below the trigger price (default 0.30), it places a single BUY order
and holds the position until market settlement.

Supports both 5-minute (btc-updown-5m-1) and 15-minute (btc-updown-paper-1)
market cadences — window size is inferred automatically from the slug.

If the current window has fewer than ``--min-window-secs`` seconds remaining
when the bot starts, it waits for the next window to open before watching.

One trade per run. Does NOT auto-follow to subsequent windows.

Usage:
    SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt \\
        uv run python scripts/snipe_bot.py --bot-id btc-updown-5m-1

Options:
    --trigger          Ask price at or below which to fire (default 0.30)
    --size             Shares to buy (default 5, Polymarket minimum)
    --min-window-secs  Skip current window if fewer than this many seconds
                       remain; wait for next window (default 120)

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
import httpx
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

_ET = ZoneInfo("America/New_York")
_CLOB_HOST = "https://clob.polymarket.com"


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
    """Bump size up if needed to meet the $1.00 CLOB minimum notional."""
    min_notional = Decimal("1.00")
    if ask * size < min_notional:
        bumped = (min_notional / ask).to_integral_value(rounding=ROUND_UP)
        click.echo(f"[{_ts()}] Size {size} -> {bumped} to meet $1 min notional")
        return bumped
    return size


def _check_clob_settlement(condition_id: str, token_id: str) -> Decimal | None:
    """Query CLOB for settlement; return our token's payout if resolved, else None.

    Settlement is confirmed when ``closed == True`` and the token entry has
    a ``winner`` field.  Payout is 1.0 (win) or 0.0 (loss) from the ``price``
    field on the token.
    """
    r = httpx.get(f"{_CLOB_HOST}/markets/{condition_id}", timeout=10)
    r.raise_for_status()
    data: dict = r.json()
    if not data.get("closed"):
        return None
    for tok in data.get("tokens", []):
        if tok.get("token_id") == token_id and "winner" in tok:
            return Decimal(str(tok["price"]))
    return None


async def _poll_settlement(
    condition_id: str,
    slug: str,
    token_id: str,
    outcome: str,
    window_end: float,
    activity: object,
) -> Decimal | None:
    """Sleep until market closes then poll CLOB every 30 s for settlement.

    Returns the payout (1 = win, 0 = loss) or None if timed out.
    Logs a ``market_settled`` audit event on success.
    """
    from src.risk.audit import KIND_MARKET_SETTLED

    remaining = max(0.0, window_end - time.time())
    if remaining > 0:
        click.echo(f"[{_ts()}] Waiting for window close in {remaining:.0f}s...")
        await asyncio.sleep(remaining + 3)  # 3 s buffer for CLOB to update

    deadline = window_end + 900  # up to 15 extra minutes for Chainlink settlement on-chain
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            payout = _check_clob_settlement(condition_id, token_id)
            if payout is not None:
                result = "WIN" if payout >= Decimal("0.5") else "LOSS"
                click.echo(
                    f"\n[{_ts()}] SETTLED ({result}) — {outcome} payout: {payout} per share"
                )
                await activity.log(  # type: ignore[attr-defined]
                    KIND_MARKET_SETTLED,
                    {
                        "outcome": outcome,
                        "token_id": token_id,
                        "payout": str(payout),
                        "slug": slug,
                        "condition_id": condition_id,
                        "result": result,
                    },
                )
                return payout
            click.echo(f"[{_ts()}] Not yet settled (attempt {attempts}) — retrying in 30s...")
        except Exception as exc:
            click.echo(f"[{_ts()}] Settlement check error: {exc} — retrying in 30s...")
        await asyncio.sleep(30)

    click.echo(f"[{_ts()}] Settlement poll timed out — check CLOB for {condition_id}.")
    return None


async def _buy(
    outcome: str,
    token_id: str,
    ask_price: Decimal,
    size: Decimal,
    activity: object,
) -> bool:
    """Place a live BUY order. Returns True if the order was accepted."""
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
        return True
    else:
        click.echo(f"[{_ts()}] Order rejected -- {result.reason}", err=True)
        click.echo(f"  raw: {result.raw}", err=True)
        await activity.log(  # type: ignore[attr-defined]
            KIND_ORDER_RESULT,
            {"accepted": False, "reason": result.reason},
            client_order_id=coid,
        )
        return False


async def _resolve_next_window(
    entry: object,
    min_window_secs: int,
) -> tuple:
    """Resolve market, skipping to the next window if too little time remains.

    Returns (resolved, up_label, up_token, dn_label, dn_token, window_end).
    If the current window has < min_window_secs remaining, sleeps until the
    next window opens and re-resolves.
    """
    from src.exchanges.market_resolver import resolve_all_outcomes

    while True:
        resolved = resolve_all_outcomes(entry.market)  # type: ignore[attr-defined]
        window_end: float = resolved.window_end or (time.time() + 900)
        secs_left = int(window_end - time.time())

        if secs_left < min_window_secs:
            wait = max(1, secs_left + 5)  # sleep through end + 5 s buffer
            click.echo(
                f"[{_ts()}] Only {secs_left}s left in current window — "
                f"waiting {wait}s for next window to open..."
            )
            await asyncio.sleep(wait)
            continue  # re-resolve into the fresh window

        outcome_map = {k.lower(): (k, v) for k, v in resolved.tokens.items()}
        up_label, up_token = outcome_map.get("up", outcome_map.get("yes", ("Up", "")))
        dn_label, dn_token = outcome_map.get("down", outcome_map.get("no", ("Down", "")))
        return resolved, up_label, up_token, dn_label, dn_token, window_end


async def _watch_and_snipe(
    bot_id: str, trigger: Decimal, size: Decimal, min_window_secs: int
) -> None:
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

    activity = await ActivityLogger.create(bot_id)
    async with activity:
        await activity.log(
            KIND_BOT_STARTED,
            {
                "script": "snipe_bot",
                "trigger": str(trigger),
                "size": str(size),
                "min_window_secs": min_window_secs,
            },
        )

        click.echo(f"\nResolving market for {bot_id!r}...")
        roster = load_roster(ROOT / "config" / "bots.yaml")
        entry = next((b for b in roster.bots if b.id == bot_id), None)
        if entry is None:
            click.echo(f"ERROR: bot {bot_id!r} not found.", err=True)
            sys.exit(1)
        if not entry.market.slug:
            click.echo("ERROR: bot must use a slug-based market.", err=True)
            sys.exit(1)

        try:
            resolved, up_label, up_token, dn_label, dn_token, window_end = (
                await _resolve_next_window(entry, min_window_secs)
            )
        except Exception as exc:
            await activity.log(KIND_ERROR, {"error": str(exc), "phase": "market_resolve"})
            raise

        if not up_token or not dn_token:
            click.echo(f"ERROR: unexpected outcomes {list(resolved.tokens.keys())}")
            await activity.log(
                KIND_ERROR,
                {"error": f"unexpected outcomes: {list(resolved.tokens.keys())}"},
            )
            sys.exit(1)

        secs_left = int(window_end - time.time())
        click.echo(f"  Slug:  {resolved.slug}")
        click.echo(f"  UP   token: ...{up_token[-8:]}")
        click.echo(f"  DOWN token: ...{dn_token[-8:]}")
        click.echo(f"  Window closes in: {secs_left // 60}m {secs_left % 60:02d}s")
        click.echo(f"  Trigger: ask <= {trigger}  |  Size: {size} shares")
        click.echo(f"\n{'─' * 70}")
        click.echo("  Watching via ClobPriceFeed... (Ctrl-C to abort)")
        click.echo(f"{'─' * 70}\n")

        await activity.log(
            KIND_MARKET_RESOLVED,
            {
                "condition_id": resolved.condition_id,
                "slug": resolved.slug,
                "outcomes": list(resolved.tokens.keys()),
                "window_end": window_end,
            },
        )

        ask_of: dict[str, Decimal] = {
            up_token: Decimal("1"),
            dn_token: Decimal("1"),
        }
        side_of: dict[str, str] = {up_token: up_label, dn_token: dn_label}
        fired = False
        fired_token: str = ""

        try:
            async with ClobPriceFeed({up_label: up_token, dn_label: dn_token}) as feed:
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

                    for t_id, outcome_name in side_of.items():
                        ask = ask_of[t_id]
                        if ask > Decimal("0") and ask <= trigger:
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
                            accepted = await _buy(outcome_name, t_id, ask, effective, activity)
                            if accepted:
                                fired = True
                                fired_token = t_id
                                click.echo(
                                    f"\n[{_ts()}] Position open — holding until settlement."
                                )
                            else:
                                click.echo(f"[{_ts()}] Order rejected — continuing to watch.")
                            break

                    if fired:
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

        if fired:
            bought_outcome = side_of.get(fired_token, "unknown")
            await _poll_settlement(
                resolved.condition_id,
                resolved.slug,
                fired_token,
                bought_outcome,
                window_end,
                activity,
            )
        else:
            click.echo(f"\n[{_ts()}] Window closed — no trigger fired. Exiting.")
            await activity.log(
                KIND_TRIGGER_MISSED,
                {
                    "trigger": str(trigger),
                    "final_up_ask": str(ask_of.get(up_token, Decimal("1"))),
                    "final_dn_ask": str(ask_of.get(dn_token, Decimal("1"))),
                },
            )

        await activity.log(KIND_BOT_STOPPED, {"reason": "done", "fired": fired})


@click.command()
@click.option(
    "--bot-id",
    default="btc-updown-5m-1",
    show_default=True,
    help="Bot ID from config/bots.yaml",
)
@click.option(
    "--trigger",
    default="0.30",
    show_default=True,
    help="Fire when ask drops to or below this price",
)
@click.option("--size", default="5", show_default=True, help="Shares to buy")
@click.option(
    "--min-window-secs",
    default=120,
    show_default=True,
    help="Skip current window if fewer than this many seconds remain; wait for next",
)
def main(bot_id: str, trigger: str, size: str, min_window_secs: int) -> None:
    """Watch UP+DOWN asks. Buy the first side that hits the trigger; hold to settlement."""
    try:
        asyncio.run(
            _watch_and_snipe(bot_id, Decimal(trigger), Decimal(size), min_window_secs)
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        click.echo("\nAborted.")


if __name__ == "__main__":
    main()
