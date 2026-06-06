"""Place a single manual order on Polymarket.

Bypasses the bot/strategy stack — no risk caps, no kill switch.
Use only for deliberate one-off trades.

Market identity is read from config/bots.yaml via --bot-id.  Dynamic/recurring
markets (slug + outcome) are resolved to the current active window at runtime.

Usage:
    SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt uv run python scripts/manual_order.py \\
        --bot-id btc-updown-paper-1 \\
        --side buy \\
        --price 0.55 \\
        --size 10

Required env:
    SOPS_AGE_KEY_FILE  path to age private key (default: ~/.config/sops/age/keys.txt)
"""

import asyncio
import hashlib
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import click
import httpx
import yaml

ROOT = Path(__file__).parent.parent


def _load_secrets() -> dict:
    enc_file = ROOT / "secrets" / "exchanges.enc.yaml"
    result = subprocess.run(
        ["sops", "--decrypt", str(enc_file)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        click.echo(f"ERROR decrypting secrets:\n{result.stderr}", err=True)
        sys.exit(1)
    return yaml.safe_load(result.stdout)


def _clob_book(token_id: str) -> tuple[str, str]:
    """Return (best_bid, best_ask) from the live CLOB order book."""
    r = httpx.get(
        "https://clob.polymarket.com/book",
        params={"token_id": token_id},
        timeout=5,
    )
    r.raise_for_status()
    data = r.json()
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    best_bid = bids[-1]["price"] if bids else "—"
    best_ask = asks[-1]["price"] if asks else "—"
    return best_bid, best_ask


def _make_client_order_id(token_id: str, side: str, price: str, size: str) -> str:
    payload = f"manual:{token_id}:{side}:{price}:{size}"
    return hashlib.blake2s(payload.encode(), digest_size=8).hexdigest()


async def _place(
    bot_id: str, side: str, price: Decimal, size: Decimal, yes: bool, outcome: str | None
) -> None:
    from src.exchanges.base import Mode, OrderIntent, Side
    from src.exchanges.market_resolver import resolve_market
    from src.exchanges.polymarket import PolymarketAdapter, load_polymarket_config
    from src.manager.config import load_roster

    roster = load_roster(ROOT / "config" / "bots.yaml")
    entry = next((b for b in roster.bots if b.id == bot_id), None)
    if entry is None:
        ids = [b.id for b in roster.bots]
        click.echo(f"ERROR: no bot {bot_id!r} in config/bots.yaml. Available: {ids}", err=True)
        sys.exit(1)

    if entry.market.exchange != "polymarket":
        click.echo(
            f"ERROR: bot {bot_id!r} uses exchange {entry.market.exchange!r}; "
            "manual_order.py only supports polymarket.",
            err=True,
        )
        sys.exit(1)

    ref = entry.market
    if outcome is not None:
        # Override the configured outcome (UP→DOWN or vice versa) for this manual trade
        ref = ref.model_copy(update={"outcome": outcome.upper()})

    click.echo(f"Resolving market for bot {bot_id!r} (outcome={ref.outcome})...")
    market = resolve_market(ref)
    click.echo(f"  condition_id : {market.market_id}")
    click.echo(f"  token_id     : ...{market.token_id[-12:]}")
    click.echo(f"  tick_size    : {market.tick_size}  min_size: {market.min_size}")

    secrets = _load_secrets()
    config = load_polymarket_config(secrets.get("polymarket", secrets))
    config = config.model_copy(update={"mode": Mode.LIVE})

    client_order_id = _make_client_order_id(market.token_id, side, str(price), str(size))

    intent = OrderIntent(
        client_order_id=client_order_id,
        market=market,
        side=Side(side.lower()),
        price=price,
        size=size,
    )

    best_bid, best_ask = _clob_book(market.token_id)
    cost = price * size

    click.echo(f"\n  token   : ...{market.token_id[-8:]}")
    click.echo(f"  CLOB    : bid={best_bid}  ask={best_ask}")
    click.echo(f"  side    : {side.upper()}")
    click.echo(f"  price   : ${price}")
    click.echo(f"  size    : {size} shares")
    click.echo(f"  cost    : ~${cost:.2f} USDC")
    click.echo(f"  order id: {client_order_id}")
    click.echo()
    if not yes:
        click.confirm("Place this LIVE order?", abort=True)

    async with PolymarketAdapter(Mode.LIVE, config) as adapter:
        result = await adapter.place(intent)

    if result.accepted:
        click.echo(f"\n✓ Order accepted — exchange id: {result.exchange_order_id}")
    else:
        click.echo(f"\n✗ Order rejected — {result.reason}", err=True)
        click.echo(f"  raw: {result.raw}", err=True)
        sys.exit(1)


@click.command()
@click.option("--bot-id", required=True, help="Bot ID from config/bots.yaml")
@click.option("--side", required=True, type=click.Choice(["buy", "sell"], case_sensitive=False))
@click.option("--price", required=True, type=Decimal, help="Limit price (0.01-0.99)")
@click.option("--size", required=True, type=Decimal, help="Shares to buy (min 5)")
@click.option(
    "--outcome", default=None, help="Override configured outcome (e.g. UP, DOWN, YES, NO)"
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def main(
    bot_id: str, side: str, price: Decimal, size: Decimal, outcome: str | None, yes: bool
) -> None:
    asyncio.run(_place(bot_id, side, price, size, yes, outcome))


if __name__ == "__main__":
    main()
