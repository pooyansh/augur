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
import json
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import click
import httpx

ROOT = Path(__file__).parent.parent
_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_CLOB_HOST = "https://clob.polymarket.com"
_GAMMA_HOST = "https://gamma-api.polymarket.com"
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


def _resolve_all_outcomes(bot_id: str) -> tuple[dict[str, str], str, float]:
    """Resolve a bot entry to all outcome tokens for the current window.

    Returns:
        tokens: dict mapping outcome name (e.g. "Up", "Down") → token_id
        label:  human-readable market slug
        window_end: unix timestamp when the current window closes
    """
    sys.path.insert(0, str(ROOT))
    from src.manager.config import load_roster

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

    slug_prefix = entry.market.slug
    if slug_prefix is None:
        # Static market — only one token configured, fall back to single-token mode
        click.echo("ERROR: static markets require --token-id; use a slug-based bot.", err=True)
        sys.exit(1)

    now = int(time.time())
    for window_ts in [(now // _WINDOW) * _WINDOW, (now // _WINDOW) * _WINDOW + _WINDOW]:
        full_slug = f"{slug_prefix}-{window_ts}"
        try:
            r = httpx.get(f"{_GAMMA_HOST}/events", params={"slug": full_slug}, timeout=10.0)
            data = r.json()
        except Exception:
            continue
        if not data:
            continue
        m = data[0]["markets"][0]
        if m.get("closed"):
            continue
        raw_ids = m.get("clobTokenIds", "[]")
        raw_outcomes = m.get("outcomes", "[]")
        token_ids: list[str] = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        outcomes: list[str] = (
            json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        )
        tokens = dict(zip(outcomes, token_ids, strict=False))
        window_end = float(window_ts + _WINDOW)
        click.echo(f"  slug         : {full_slug}")
        click.echo(f"  condition_id : {m.get('conditionId', '?')}")
        click.echo(f"  outcomes     : {list(tokens.keys())}")
        click.echo(f"  window ends  : {_countdown(window_end)}")
        return tokens, slug_prefix, window_end

    click.echo(f"ERROR: no active window found for slug prefix {slug_prefix!r}", err=True)
    sys.exit(1)


def _fetch_book(token_id: str) -> tuple[Decimal, Decimal]:
    """Fetch best bid/ask from REST /book (never Gamma)."""
    try:
        r = httpx.get(f"{_CLOB_HOST}/book", params={"token_id": token_id}, timeout=5.0)
        r.raise_for_status()
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        bid = Decimal(str(bids[-1]["price"])) if bids else Decimal("0")
        ask = Decimal(str(asks[-1]["price"])) if asks else Decimal("1")
        return bid, ask
    except Exception:
        return Decimal("0"), Decimal("1")


def _book_line(
    label: str,
    window_end: float,
    up_bid: Decimal,
    up_ask: Decimal,
    dn_bid: Decimal,
    dn_ask: Decimal,
    event: str = "BOOK",
) -> str:
    return (
        f"[{_ts()}] {event:<5}  "
        f"UP  bid={up_bid:.2f} ask={up_ask:.2f} "
        f"buy@{up_ask:.2f} sell@{up_bid:.2f}  │  "
        f"DOWN bid={dn_bid:.2f} ask={dn_ask:.2f} "
        f"buy@{dn_ask:.2f} sell@{dn_bid:.2f}  "
        f"│  closes {_countdown(window_end)}"
    )


# ---------------------------------------------------------------------------
# Main stream loop
# ---------------------------------------------------------------------------


async def _stream(bot_id: str) -> None:
    import websockets

    click.echo(f"\nResolving market for bot {bot_id!r}...")
    tokens, label, window_end = _resolve_all_outcomes(bot_id)

    # Find UP and DOWN token IDs (case-insensitive key lookup)
    outcome_map = {k.lower(): (k, v) for k, v in tokens.items()}
    up_label, up_token = outcome_map.get("up", outcome_map.get("yes", ("Up", "")))
    dn_label, dn_token = outcome_map.get("down", outcome_map.get("no", ("Down", "")))

    if not up_token or not dn_token:
        click.echo(
            f"ERROR: could not identify UP/DOWN tokens from outcomes {list(tokens.keys())}",
            err=True,
        )
        sys.exit(1)

    # Seed initial book from REST
    up_bid, up_ask = _fetch_book(up_token)
    dn_bid, dn_ask = _fetch_book(dn_token)

    click.echo(f"\n{'─' * 110}")
    click.echo(
        f"  {label}  │  "
        f"{up_label}: bid={up_bid:.2f} ask={up_ask:.2f}  │  "
        f"{dn_label}: bid={dn_bid:.2f} ask={dn_ask:.2f}  │  "
        f"closes in {_countdown(window_end)} ET"
    )
    click.echo(f"{'─' * 110}\n")

    # map token_id → which side
    side_of: dict[str, str] = {up_token: up_label, dn_token: dn_label}
    # per-token state
    bid_of: dict[str, Decimal] = {up_token: up_bid, dn_token: dn_bid}
    ask_of: dict[str, Decimal] = {up_token: up_ask, dn_token: dn_ask}

    backoff = 1.0

    while True:
        # Window expiry — re-resolve and restart
        if time.time() >= window_end:
            click.echo(f"\n{'═' * 110}")
            click.echo(f"  ⏰  Window closed — resolving next window for {label} ...")
            click.echo(f"{'═' * 110}\n")
            tokens, label, window_end = _resolve_all_outcomes(bot_id)
            outcome_map = {k.lower(): (k, v) for k, v in tokens.items()}
            up_label, up_token = outcome_map.get("up", outcome_map.get("yes", ("Up", "")))
            dn_label, dn_token = outcome_map.get("down", outcome_map.get("no", ("Down", "")))
            up_bid, up_ask = _fetch_book(up_token)
            dn_bid, dn_ask = _fetch_book(dn_token)
            side_of = {up_token: up_label, dn_token: dn_label}
            bid_of = {up_token: up_bid, dn_token: dn_bid}
            ask_of = {up_token: up_ask, dn_token: dn_ask}
            click.echo(_book_line(label, window_end, up_bid, up_ask, dn_bid, dn_ask, "INIT"))

        try:
            async with websockets.connect(_WS_URL) as ws:
                backoff = 1.0
                sub = json.dumps({"type": "market", "assets_ids": [up_token, dn_token]})
                await ws.send(sub)

                async for raw in ws:
                    if not isinstance(raw, str):
                        continue

                    # Check window expiry on every message
                    if time.time() >= window_end:
                        break  # break to outer loop to re-resolve

                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    changed = False

                    if isinstance(payload, list):
                        # WS initial snapshot
                        for item in payload:
                            t_id = item.get("asset_id", "")
                            if t_id not in side_of:
                                continue
                            bids = item.get("bids", [])
                            asks = item.get("asks", [])
                            if bids:
                                bid_of[t_id] = max(
                                    Decimal(str(b["price"])) for b in bids if b.get("price")
                                )
                            if asks:
                                ask_of[t_id] = min(
                                    Decimal(str(a["price"])) for a in asks if a.get("price")
                                )
                            changed = True
                        if changed:
                            click.echo(
                                _book_line(
                                    label,
                                    window_end,
                                    bid_of[up_token],
                                    ask_of[up_token],
                                    bid_of[dn_token],
                                    ask_of[dn_token],
                                    "SNAP",
                                )
                            )

                    elif isinstance(payload, dict):
                        ev = payload.get("event_type", "")
                        t_id = payload.get("asset_id", "")

                        if ev == "price_change" and t_id in side_of:
                            bids = payload.get("bids", [])
                            asks = payload.get("asks", [])
                            if bids:
                                bid_of[t_id] = max(
                                    Decimal(str(b["price"])) for b in bids if b.get("price")
                                )
                            if asks:
                                ask_of[t_id] = min(
                                    Decimal(str(a["price"])) for a in asks if a.get("price")
                                )
                            click.echo(
                                _book_line(
                                    label,
                                    window_end,
                                    bid_of[up_token],
                                    ask_of[up_token],
                                    bid_of[dn_token],
                                    ask_of[dn_token],
                                    "BOOK",
                                )
                            )

                        elif ev in ("last_trade_price", "trade") and t_id in side_of:
                            side = side_of[t_id]
                            price = payload.get("price", "?")
                            size = payload.get("size", "?")
                            tside = payload.get("side", "?")
                            click.echo(
                                f"[{_ts()}] TRADE  {side:<4}  "
                                f"price={price}  size={size}  side={tside}"
                            )

        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            click.echo(f"[{_ts()}] disconnected — {exc}  (reconnect in {backoff:.0f}s)", err=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


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
