"""Stream live order-book updates for a Polymarket market to stdout.

Connects to the Polymarket WebSocket subscription endpoint and prints
real-time best-bid / best-ask updates for the specified token.  Market
identity follows the same resolution path as manual_order.py — either a
bot-id from config/bots.yaml or a raw token-id.

Usage (by bot-id, resolves slug → token automatically):
    uv run python scripts/watch_market.py --bot-id btc-updown-paper-1

Usage (by raw token-id):
    uv run python scripts/watch_market.py --token-id 71321045...

Press Ctrl-C to exit.

Required env (for --bot-id path):
    None — market resolution uses public Gamma/CLOB APIs only.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import click
import httpx

ROOT = Path(__file__).parent.parent
_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_CLOB_HOST = "https://clob.polymarket.com"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_token_from_bot(bot_id: str, outcome_override: str | None = None) -> tuple[str, str]:
    """Resolve a bot-id from bots.yaml to (token_id, label).

    Args:
        bot_id: Bot identifier matching an entry in config/bots.yaml.

    Returns:
        Tuple of (token_id, label) where label is a human-readable market name.

    Raises:
        SystemExit: If the bot is not found or is not a Polymarket bot.
    """
    sys.path.insert(0, str(ROOT))
    from src.exchanges.market_resolver import resolve_market
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
            "watch_market.py only supports polymarket.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"Resolving market for bot {bot_id!r}...")
    ref = entry.market
    if outcome_override:
        ref = ref.model_copy(update={"outcome": outcome_override.upper()})
    market = resolve_market(ref)
    label = f"{ref.slug or ref.market_id} / {ref.outcome or ''}"
    click.echo(f"  condition_id : {market.market_id}")
    click.echo(f"  token_id     : ...{market.token_id[-12:]}")
    click.echo(f"  tick_size    : {market.tick_size}  min_size: {market.min_size}")
    return market.token_id, label.strip(" /")


def _fetch_initial_book(token_id: str) -> tuple[Decimal, Decimal]:
    """Fetch the current best bid and ask from the CLOB REST API.

    Uses clob.polymarket.com/book (not Gamma) for live prices per project rules.

    Args:
        token_id: ERC-1155 outcome token ID.

    Returns:
        (best_bid, best_ask) as Decimal values.  Returns (0, 1) on error.
    """
    try:
        r = httpx.get(
            f"{_CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=5.0,
        )
        r.raise_for_status()
        data: dict = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        best_bid = Decimal(str(bids[-1]["price"])) if bids else Decimal("0")
        best_ask = Decimal(str(asks[-1]["price"])) if asks else Decimal("1")
        return best_bid, best_ask
    except Exception as exc:
        click.echo(f"  [warn] Could not fetch initial book: {exc}", err=True)
        return Decimal("0"), Decimal("1")


def _ts() -> str:
    return datetime.now(tz=UTC).strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# WS stream
# ---------------------------------------------------------------------------


async def _stream(token_id: str, label: str) -> None:
    """Subscribe to Polymarket WS and print book updates forever.

    Reconnects with exponential backoff on disconnection.

    Args:
        token_id: ERC-1155 token ID to subscribe to.
        label: Human-readable market label for display only.
    """
    import websockets

    best_bid: Decimal = Decimal("0")
    best_ask: Decimal = Decimal("1")
    backoff = 1.0

    # Seed from REST before the WS connect so the first line is meaningful.
    best_bid, best_ask = _fetch_initial_book(token_id)
    click.echo(
        f"\n[{_ts()}] initial snapshot  bid={best_bid:.4f}  ask={best_ask:.4f}  "
        f"spread={best_ask - best_bid:.4f}"
    )

    click.echo(f"\nStreaming {label} (token ...{token_id[-10:]})  Ctrl-C to quit\n")

    while True:
        try:
            async with websockets.connect(_WS_URL) as ws:
                backoff = 1.0
                sub = json.dumps({"type": "market", "assets_ids": [token_id]})
                await ws.send(sub)

                async for raw in ws:
                    if not isinstance(raw, str):
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if isinstance(payload, list):
                        # Initial WS snapshot — update best bid/ask from book state
                        for item in payload:
                            t_id = item.get("asset_id", "")
                            if t_id != token_id:
                                continue
                            bids = item.get("bids", [])
                            asks = item.get("asks", [])
                            if bids:
                                best_bid = max(
                                    Decimal(str(b["price"])) for b in bids if b.get("price")
                                )
                            if asks:
                                best_ask = min(
                                    Decimal(str(a["price"])) for a in asks if a.get("price")
                                )
                        click.echo(
                            f"[{_ts()}] ws snapshot       bid={best_bid:.4f}  "
                            f"ask={best_ask:.4f}  spread={best_ask - best_bid:.4f}"
                        )

                    elif isinstance(payload, dict):
                        event_type = payload.get("event_type", "")
                        t_id = payload.get("asset_id", "")

                        if event_type == "price_change" and t_id == token_id:
                            bids = payload.get("bids", [])
                            asks = payload.get("asks", [])
                            if bids:
                                best_bid = max(
                                    Decimal(str(b["price"])) for b in bids if b.get("price")
                                )
                            if asks:
                                best_ask = min(
                                    Decimal(str(a["price"])) for a in asks if a.get("price")
                                )
                            click.echo(
                                f"[{_ts()}] price_change       bid={best_bid:.4f}  "
                                f"ask={best_ask:.4f}  spread={best_ask - best_bid:.4f}"
                            )

                        elif event_type in ("last_trade_price", "trade") and t_id == token_id:
                            price = payload.get("price", "?")
                            size = payload.get("size", "?")
                            side = payload.get("side", "?")
                            click.echo(
                                f"[{_ts()}] TRADE              price={price}  "
                                f"size={size}  side={side}"
                            )

                        elif event_type == "order_cancelled" and t_id == token_id:
                            click.echo(f"[{_ts()}] order_cancelled    id={payload.get('id', '?')}")

                        elif event_type == "order_rejected" and t_id == token_id:
                            click.echo(
                                f"[{_ts()}] order_rejected     reason={payload.get('reason', '?')}"
                            )

                        # Silently skip unrelated token events (other subscriptions on the feed)

        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            click.echo(
                f"[{_ts()}] WS disconnected — {exc}  (reconnect in {backoff:.0f}s)",
                err=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


@click.command()
@click.option("--bot-id", default=None, help="Bot ID from config/bots.yaml (resolves market)")
@click.option("--token-id", default=None, help="Raw ERC-1155 token ID (skips resolution)")
@click.option("--outcome", default=None, help="Override configured outcome (UP/DOWN/YES/NO)")
def main(bot_id: str | None, token_id: str | None, outcome: str | None) -> None:
    """Stream live Polymarket order-book updates to stdout.

    Provide exactly one of --bot-id or --token-id.
    """
    if bot_id is None and token_id is None:
        click.echo("ERROR: provide --bot-id or --token-id", err=True)
        sys.exit(1)
    if bot_id is not None and token_id is not None:
        click.echo("ERROR: provide only one of --bot-id or --token-id", err=True)
        sys.exit(1)

    if bot_id is not None:
        resolved_token, label = _resolve_token_from_bot(bot_id, outcome_override=outcome)
    else:
        assert token_id is not None
        resolved_token = token_id
        label = f"token ...{token_id[-10:]}"

    try:
        asyncio.run(_stream(resolved_token, label))
    except (KeyboardInterrupt, asyncio.CancelledError):
        click.echo("\nStopped.")


if __name__ == "__main__":
    main()
