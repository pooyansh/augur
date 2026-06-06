"""One-window dip-buyer for Polymarket BTC Up/Down 15m markets.

Watches the live CLOB WebSocket for both UP and DOWN outcomes.
When either side's ask drops to or below the trigger price (default 0.30),
it places a single BUY order and exits.

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
import json
import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import click
import httpx
import yaml

ROOT = Path(__file__).parent.parent
_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_GAMMA_HOST = "https://gamma-api.polymarket.com"
_CLOB_HOST = "https://clob.polymarket.com"
_ET = ZoneInfo("America/New_York")
_WINDOW = 900


def _ts() -> str:
    return datetime.now(tz=_ET).strftime("%H:%M:%S")


def _load_secrets() -> dict:
    enc = ROOT / "secrets" / "exchanges.enc.yaml"
    r = subprocess.run(["sops", "--decrypt", str(enc)], capture_output=True, text=True)
    if r.returncode != 0:
        click.echo(f"ERROR decrypting secrets:\n{r.stderr}", err=True)
        sys.exit(1)
    return yaml.safe_load(r.stdout)


def _resolve_both_tokens(bot_id: str) -> tuple[dict[str, str], str, float]:
    """Return ({outcome: token_id}, slug, window_end) for the current active window."""
    sys.path.insert(0, str(ROOT))
    from src.manager.config import load_roster

    roster = load_roster(ROOT / "config" / "bots.yaml")
    entry = next((b for b in roster.bots if b.id == bot_id), None)
    if entry is None:
        click.echo(f"ERROR: bot {bot_id!r} not found.", err=True)
        sys.exit(1)

    slug_prefix = entry.market.slug
    if not slug_prefix:
        click.echo("ERROR: bot must use a slug-based market.", err=True)
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
        return tokens, slug_prefix, window_end

    click.echo(f"ERROR: no active window for {slug_prefix!r}.", err=True)
    sys.exit(1)


async def _buy(outcome: str, token_id: str, ask_price: Decimal, size: Decimal) -> None:
    """Place a live BUY order using the Polymarket adapter."""
    sys.path.insert(0, str(ROOT))
    from src.exchanges.base import Market, Mode, OrderIntent, Side
    from src.exchanges.polymarket import PolymarketAdapter, load_polymarket_config

    secrets = _load_secrets()
    config = load_polymarket_config(secrets.get("polymarket", secrets))

    # Build a minimal Market object — tick_size/min_size not needed for signing
    market = Market(
        market_id="",  # not used by the adapter for order placement
        token_id=token_id,
        tick_size=Decimal("0.01"),
        min_size=Decimal("5"),
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

    click.echo(f"[{_ts()}] 🔫 FIRING — BUY {size} {outcome} @ {ask_price}  (coid {coid})")

    async with PolymarketAdapter(Mode.LIVE, config) as adapter:
        result = await adapter.place(intent)

    if result.accepted:
        click.echo(f"[{_ts()}] ✓ Order accepted — exchange id: {result.exchange_order_id}")
    else:
        click.echo(f"[{_ts()}] ✗ Order rejected — {result.reason}", err=True)
        click.echo(f"  raw: {result.raw}", err=True)


async def _watch_and_snipe(bot_id: str, trigger: Decimal, size: Decimal) -> None:
    import websockets

    click.echo(f"\nResolving current window for {bot_id!r}...")
    tokens, _label, window_end = _resolve_both_tokens(bot_id)

    outcome_map = {k.lower(): (k, v) for k, v in tokens.items()}
    up_label, up_token = outcome_map.get("up", outcome_map.get("yes", ("Up", "")))
    dn_label, dn_token = outcome_map.get("down", outcome_map.get("no", ("Down", "")))

    if not up_token or not dn_token:
        click.echo(f"ERROR: unexpected outcomes {list(tokens.keys())}", err=True)
        sys.exit(1)

    side_of = {up_token: up_label, dn_token: dn_label}
    ask_of: dict[str, Decimal] = {up_token: Decimal("1"), dn_token: Decimal("1")}

    secs_left = int(window_end - time.time())
    click.echo(f"  UP   token: ...{up_token[-8:]}")
    click.echo(f"  DOWN token: ...{dn_token[-8:]}")
    click.echo(f"  Window closes in: {secs_left // 60}m {secs_left % 60:02d}s")
    click.echo(f"  Trigger: ask ≤ {trigger}  |  Size: {size} shares")
    click.echo(f"\n{'─' * 70}")
    click.echo("  Watching... (Ctrl-C to abort)")
    click.echo(f"{'─' * 70}\n")

    fired = False
    backoff = 1.0

    while not fired:
        if time.time() >= window_end:
            click.echo(f"\n[{_ts()}] Window closed — no trigger fired. Exiting.")
            return

        try:
            async with websockets.connect(_WS_URL) as ws:
                backoff = 1.0
                sub = json.dumps({"type": "market", "assets_ids": [up_token, dn_token]})
                await ws.send(sub)

                async for raw in ws:
                    if fired or time.time() >= window_end:
                        break

                    if not isinstance(raw, str):
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # Parse ask prices from snapshot or price_change
                    updates: list[tuple[str, list, list]] = []

                    if isinstance(payload, list):
                        for item in payload:
                            t_id = item.get("asset_id", "")
                            if t_id in side_of:
                                updates.append((t_id, item.get("bids", []), item.get("asks", [])))

                    elif isinstance(payload, dict):
                        ev = payload.get("event_type", "")
                        t_id = payload.get("asset_id", "")
                        if ev == "price_change" and t_id in side_of:
                            updates.append((t_id, payload.get("bids", []), payload.get("asks", [])))

                    for t_id, _, asks in updates:
                        if not asks:
                            continue
                        new_ask = min(Decimal(str(a["price"])) for a in asks if a.get("price"))
                        old_ask = ask_of[t_id]
                        ask_of[t_id] = new_ask
                        outcome_name = side_of[t_id]

                        # Log every price update so the user can see the stream
                        up_ask = ask_of[up_token]
                        dn_ask = ask_of[dn_token]
                        moved = (
                            f"  ← {outcome_name} {old_ask:.2f}→{new_ask:.2f}"
                            if new_ask != old_ask
                            else ""
                        )
                        click.echo(f"[{_ts()}]  UP ask={up_ask:.2f}  DOWN ask={dn_ask:.2f}{moved}")

                        # Trigger check
                        if new_ask <= trigger and not fired:
                            fired = True
                            click.echo(
                                f"\n[{_ts()}] TRIGGER — {outcome_name} ask={new_ask:.2f}"
                                f" <= {trigger}"
                            )
                            await _buy(outcome_name, t_id, new_ask, size)
                            click.echo(f"\n[{_ts()}] Done. Holding until window closes.")
                            return

        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            click.echo(f"[{_ts()}] WS error: {exc}  (reconnect in {backoff:.0f}s)", err=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


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
    """Watch UP+DOWN asks. Buy 5 shares of the first side that hits the trigger."""
    try:
        asyncio.run(_watch_and_snipe(bot_id, Decimal(trigger), Decimal(size)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        click.echo("\nAborted.")


if __name__ == "__main__":
    main()
