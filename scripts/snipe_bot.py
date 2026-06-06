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
import contextlib
import hashlib
import json
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

    click.echo(f"[{_ts()}] 🔫 FIRING — BUY {size} {outcome} @ {ask_price}  (coid {coid})")

    async with PolymarketAdapter(Mode.LIVE, config) as adapter:
        result = await adapter.place(intent)

    if result.accepted:
        click.echo(f"[{_ts()}] ✓ Order accepted — exchange id: {result.exchange_order_id}")
    else:
        click.echo(f"[{_ts()}] ✗ Order rejected — {result.reason}", err=True)
        click.echo(f"  raw: {result.raw}", err=True)


def _print(msg: str) -> None:
    """Print with immediate flush — works correctly in both foreground and piped mode."""
    click.echo(msg)
    sys.stdout.flush()


def _check_trigger(
    ask_of: dict[str, Decimal],
    side_of: dict[str, str],
    trigger: Decimal,
    fired: bool,
) -> tuple[str, str, Decimal] | None:
    """Return (outcome_name, token_id, ask) if any token is at or below trigger."""
    if fired:
        return None
    for t_id, outcome_name in side_of.items():
        ask = ask_of[t_id]
        if ask > Decimal("0") and ask <= trigger:
            return outcome_name, t_id, ask
    return None


async def _rest_poll_loop(
    up_token: str,
    dn_token: str,
    ask_of: dict[str, Decimal],
    side_of: dict[str, str],
    trigger: Decimal,
    size: Decimal,
    window_end: float,
    fired_flag: list[bool],
) -> None:
    """Poll REST /book every 15 s as a fallback trigger — catches moves WS may miss."""
    while not fired_flag[0] and time.time() < window_end:
        await asyncio.sleep(15)
        if fired_flag[0] or time.time() >= window_end:
            break
        for t_id in (up_token, dn_token):
            try:
                r = httpx.get(f"{_CLOB_HOST}/book", params={"token_id": t_id}, timeout=5.0)
                data = r.json()
                asks = data.get("asks", [])
                if not asks:
                    continue
                new_ask = min(Decimal(str(a["price"])) for a in asks if a.get("price"))
                old_ask = ask_of[t_id]
                if new_ask != old_ask:
                    ask_of[t_id] = new_ask
                    up_ask = ask_of[up_token]
                    dn_ask = ask_of[dn_token]
                    _print(
                        f"[{_ts()}]  UP ask={up_ask:.2f}  DOWN ask={dn_ask:.2f}"
                        f"  ← REST poll: {side_of[t_id]} {old_ask:.2f}→{new_ask:.2f}"
                    )
            except Exception:
                pass

        hit = _check_trigger(ask_of, side_of, trigger, fired_flag[0])
        if hit:
            fired_flag[0] = True
            outcome_name, t_id, ask = hit
            effective_size = _effective_size(size, ask)
            _print(f"\n[{_ts()}] TRIGGER (REST poll) — {outcome_name} ask={ask:.2f} <= {trigger}")
            await _buy(outcome_name, t_id, ask, effective_size)
            _print(f"\n[{_ts()}] Done. Holding until window closes.")
            return


def _effective_size(size: Decimal, ask: Decimal) -> Decimal:
    """Bump size up if needed to meet the $1.00 CLOB minimum notional."""
    min_notional = Decimal("1.00")
    if ask * size < min_notional:
        bumped = (min_notional / ask).to_integral_value(rounding=ROUND_UP)
        _print(f"[{_ts()}] Size {size} → {bumped} to meet $1 min notional")
        return bumped
    return size


async def _watch_and_snipe(bot_id: str, trigger: Decimal, size: Decimal) -> None:
    import websockets

    _print(f"\nResolving current window for {bot_id!r}...")
    tokens, _label, window_end = _resolve_both_tokens(bot_id)

    outcome_map = {k.lower(): (k, v) for k, v in tokens.items()}
    up_label, up_token = outcome_map.get("up", outcome_map.get("yes", ("Up", "")))
    dn_label, dn_token = outcome_map.get("down", outcome_map.get("no", ("Down", "")))

    if not up_token or not dn_token:
        _print(f"ERROR: unexpected outcomes {list(tokens.keys())}")
        sys.exit(1)

    side_of = {up_token: up_label, dn_token: dn_label}
    ask_of: dict[str, Decimal] = {up_token: Decimal("1"), dn_token: Decimal("1")}
    fired_flag = [False]  # mutable so REST poller and WS loop share state

    secs_left = int(window_end - time.time())
    _print(f"  UP   token: ...{up_token[-8:]}")
    _print(f"  DOWN token: ...{dn_token[-8:]}")
    _print(f"  Window closes in: {secs_left // 60}m {secs_left % 60:02d}s")
    _print(f"  Trigger: ask <= {trigger}  |  Size: {size} shares")
    _print(f"\n{'─' * 70}")
    _print("  Watching via WS + REST poll every 15s... (Ctrl-C to abort)")
    _print(f"{'─' * 70}\n")

    # Start REST polling fallback in background
    poll_task = asyncio.create_task(
        _rest_poll_loop(up_token, dn_token, ask_of, side_of, trigger, size, window_end, fired_flag)
    )

    backoff = 1.0

    try:
        while not fired_flag[0]:
            if time.time() >= window_end:
                _print(f"\n[{_ts()}] Window closed — no trigger fired. Exiting.")
                return

            try:
                async with websockets.connect(_WS_URL) as ws:
                    backoff = 1.0
                    sub = json.dumps({"type": "market", "assets_ids": [up_token, dn_token]})
                    await ws.send(sub)

                    async for raw in ws:
                        if fired_flag[0] or time.time() >= window_end:
                            break
                        if not isinstance(raw, str):
                            continue
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        updates: list[tuple[str, list, list]] = []

                        if isinstance(payload, list):
                            for item in payload:
                                t_id = item.get("asset_id", "")
                                if t_id in side_of:
                                    updates.append(
                                        (t_id, item.get("bids", []), item.get("asks", []))
                                    )
                        elif isinstance(payload, dict):
                            ev = payload.get("event_type", "")
                            t_id = payload.get("asset_id", "")
                            if ev == "price_change" and t_id in side_of:
                                updates.append(
                                    (t_id, payload.get("bids", []), payload.get("asks", []))
                                )

                        for t_id, _, asks in updates:
                            if not asks:
                                continue
                            new_ask = min(Decimal(str(a["price"])) for a in asks if a.get("price"))
                            old_ask = ask_of[t_id]
                            ask_of[t_id] = new_ask

                            up_ask = ask_of[up_token]
                            dn_ask = ask_of[dn_token]
                            moved = (
                                f"  ← {side_of[t_id]} {old_ask:.2f}→{new_ask:.2f}"
                                if new_ask != old_ask
                                else ""
                            )
                            _print(f"[{_ts()}]  UP ask={up_ask:.2f}  DOWN ask={dn_ask:.2f}{moved}")

                            hit = _check_trigger(ask_of, side_of, trigger, fired_flag[0])
                            if hit:
                                fired_flag[0] = True
                                outcome_name, hit_id, ask = hit
                                effective = _effective_size(size, ask)
                                _print(
                                    f"\n[{_ts()}] TRIGGER (WS) — {outcome_name}"
                                    f" ask={ask:.2f} <= {trigger}"
                                )
                                await _buy(outcome_name, hit_id, ask, effective)
                                _print(f"\n[{_ts()}] Done. Holding until window closes.")
                                return

            except asyncio.CancelledError:
                raise
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                _print(f"[{_ts()}] WS error: {exc}  (reconnect in {backoff:.0f}s)")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task


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
