"""One-off live verification: fire a real snipe trade, then independently
evaluate the ``polymarket.btc_up_or_down_5m.price_compare`` winning rule
against live BTC price data through the window, and compare its final
ruling against the market's own official settlement outcome.

This is a THROWAWAY manual verification script (not part of the supervised
bot fleet). It reuses ``scripts/snipe_bot.py``'s window-resolution, live-ask
watching, buy, and settlement-polling logic directly by import -- it does
NOT reimplement or modify any of that logic.

Real money: places one real live BUY order (~$1.50, 5 shares) via
``PolymarketAdapter`` in LIVE mode when the trigger fires.

Usage:
    SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt \\
        uv run python scripts/verify_winning_rule.py --bot-id btc-updown-5m-1

Required env:
    SOPS_AGE_KEY_FILE  path to age private key (~/.config/sops/age/keys.txt)
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import click

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts.snipe_bot import (  # noqa: E402
    _buy,
    _check_clob_settlement,
    _effective_size,
    _poll_settlement,
    _resolve_next_window,
    _ts,
)

_PRICE_POLL_SECS = 12.0


@dataclass
class RulingRow:
    """One timeline row: a live BTC price observation plus its provisional ruling."""

    ts: datetime
    price: Decimal
    ruling: str


async def _fetch_btc_price() -> tuple[Decimal, str] | None:
    """Fetch live BTC/USD price the same way the ``btc_15min`` signal does.

    Tries Coingecko first, falls back to Binance -- same source order as
    ``src.signals.btc_15min.Btc15Min``. Returns ``(price, source_name)`` or
    ``None`` if both sources fail.
    """
    from src.signals.btc_15min import Btc15Min, BtcBinanceSource, BtcCoingeckoSource

    signal = Btc15Min({})
    for source_cls in (BtcCoingeckoSource, BtcBinanceSource):
        source = source_cls()
        try:
            raw = await source.fetch({})
            parsed = signal.parse(source_cls.name, raw)
            return Decimal(parsed["price_usd"]), parsed["source"]
        except Exception as exc:  # noqa: BLE001 -- fall through to next source
            click.echo(f"[{_ts()}] price fetch via {source_cls.name} failed: {exc}", err=True)
    return None


def _snapshot_for(price: Decimal, source: str) -> object:
    """Build a ``SignalSnapshot`` carrying only the ``btc_15min`` sample."""
    from src.signals.base import SignalSnapshot

    return SignalSnapshot(
        samples={
            "btc_15min": {
                "price_usd": str(price),
                "source": source,
                "source_ts": datetime.now(tz=UTC).isoformat(),
            }
        },
        received_at=datetime.now(tz=UTC),
        stale=frozenset(),
    )


def _evaluate_rule(rule: object, position: object, price: Decimal, source: str) -> str:
    """Evaluate ``PriceCompare`` for the given live price. Returns the ruling value."""
    from src.rules.base import WinningRuleContext

    ctx = WinningRuleContext(
        position=position,  # type: ignore[arg-type]
        signals=_snapshot_for(price, source),  # type: ignore[arg-type]
        now=datetime.now(tz=UTC),
        params={},
    )
    return rule.evaluate(ctx).value  # type: ignore[attr-defined]


async def _price_poll_loop(
    latest: dict[str, tuple[Decimal, str, datetime]],
    stop_event: asyncio.Event,
) -> None:
    """Continuously poll live BTC price and stash the latest sample in ``latest``."""
    while not stop_event.is_set():
        result = await _fetch_btc_price()
        if result is not None:
            price, source = result
            now = datetime.now(tz=UTC)
            latest["sample"] = (price, source, now)
            click.echo(f"[{_ts()}] BTC price={price} (source={source})")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_PRICE_POLL_SECS)
        except asyncio.TimeoutError:
            pass


async def _watch_price_ruling_loop(
    latest: dict[str, tuple[Decimal, str, datetime]],
    rule: object,
    position: object,
    window_end: float,
    timeline: list[RulingRow],
) -> None:
    """After entry: poll price + evaluate the rule each cadence until window closes."""
    while time.time() < window_end:
        result = await _fetch_btc_price()
        if result is not None:
            price, source = result
            latest["sample"] = (price, source, datetime.now(tz=UTC))
            ruling = _evaluate_rule(rule, position, price, source)
            row = RulingRow(ts=datetime.now(tz=UTC), price=price, ruling=ruling)
            timeline.append(row)
            click.echo(
                f"[{_ts()}] BTC={price}  ruling={ruling.upper()}  "
                f"(entry={position.entry_reference}, side={position.side})"  # type: ignore[attr-defined]
            )
        remaining = window_end - time.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(_PRICE_POLL_SECS, max(0.5, remaining)))


async def _run(bot_id: str, trigger: Decimal, size: Decimal, min_window_secs: int) -> None:
    from src.exchanges.market_resolver import resolve_all_outcomes
    from src.feeds.clob_feed import ClobPriceFeed
    from src.infra.activity_logger import ActivityLogger
    from src.manager.config import load_roster
    from src.risk.audit import KIND_MARKET_RESOLVED, KIND_PROVISIONAL_RULING, KIND_TRIGGER_FIRED
    from src.rules.base import PositionState
    from src.rules.registry import rules as rule_registry

    rule_registry.autodiscover()
    rule_cls = rule_registry.get("polymarket.btc_up_or_down_5m.price_compare")
    rule = rule_cls()

    activity = await ActivityLogger.create(f"{bot_id}-verify-winning-rule")
    async with activity:
        click.echo(f"\nResolving market for {bot_id!r}...")
        roster = load_roster(ROOT / "config" / "bots.yaml")
        entry = next((b for b in roster.bots if b.id == bot_id), None)
        if entry is None:
            click.echo(f"ERROR: bot {bot_id!r} not found.", err=True)
            sys.exit(1)

        resolved, up_label, up_token, dn_label, dn_token, window_end = (
            await _resolve_next_window(entry, min_window_secs)
        )
        if not up_token or not dn_token:
            click.echo(f"ERROR: unexpected outcomes {list(resolved.tokens.keys())}")
            sys.exit(1)

        secs_left = int(window_end - time.time())
        click.echo(f"  Slug:         {resolved.slug}")
        click.echo(f"  condition_id: {resolved.condition_id}")
        click.echo(f"  UP   token:   {up_token}")
        click.echo(f"  DOWN token:   {dn_token}")
        click.echo(f"  Window closes in: {secs_left // 60}m {secs_left % 60:02d}s")
        click.echo(f"  Trigger: ask <= {trigger}  |  Size: {size} shares")
        click.echo(f"\n{'─' * 70}")
        click.echo("  Watching asks + live BTC price in parallel... (Ctrl-C to abort)")
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

        # Start the background price-polling loop (runs until we stop it).
        latest_price: dict[str, tuple[Decimal, str, datetime]] = {}
        stop_price_poll = asyncio.Event()
        price_task = asyncio.create_task(_price_poll_loop(latest_price, stop_price_poll))

        ask_of: dict[str, Decimal] = {up_token: Decimal("1"), dn_token: Decimal("1")}
        side_of: dict[str, str] = {up_token: up_label, dn_token: dn_label}
        fired = False
        fired_token = ""
        entry_reference: Decimal | None = None
        entry_at: datetime | None = None
        fired_size: Decimal = size

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
                            # Use the most recently observed live BTC price as
                            # entry_reference -- do NOT block firing on a fresh fetch.
                            sample = latest_price.get("sample")
                            if sample is None:
                                click.echo(
                                    f"[{_ts()}] No BTC price observed yet -- fetching once "
                                    "synchronously before firing (unavoidable delay)."
                                )
                                fetched = await _fetch_btc_price()
                                if fetched is None:
                                    click.echo(
                                        f"[{_ts()}] Could not obtain BTC price -- aborting fire.",
                                        err=True,
                                    )
                                    continue
                                price, source = fetched
                                sample = (price, source, datetime.now(tz=UTC))
                                latest_price["sample"] = sample
                            entry_reference = sample[0]
                            entry_at = datetime.now(tz=UTC)

                            accepted = await _buy(outcome_name, t_id, ask, effective, activity)
                            if accepted:
                                fired = True
                                fired_token = t_id
                                fired_size = effective
                                click.echo(
                                    f"\n[{_ts()}] Position open -- entry_reference="
                                    f"{entry_reference} (side={outcome_name}) -- "
                                    "holding until settlement."
                                )
                            else:
                                click.echo(f"[{_ts()}] Order rejected -- continuing to watch.")
                                entry_reference = None
                                entry_at = None
                            break
                    if fired:
                        break
        finally:
            stop_price_poll.set()
            price_task.cancel()
            try:
                await price_task
            except (asyncio.CancelledError, Exception):
                pass

        if not fired:
            click.echo(f"\n[{_ts()}] Window closed -- no trigger fired. Exiting.")
            return

        assert entry_reference is not None and entry_at is not None
        side = side_of.get(fired_token, "unknown").upper()
        position = PositionState(
            market_id=resolved.condition_id,
            side=side,
            entry_reference=entry_reference,
            entry_at=entry_at,
            size=fired_size,
        )

        click.echo(f"\n{'─' * 70}")
        click.echo(
            f"  ENTRY  side={side}  entry_reference={entry_reference}  "
            f"entry_at={entry_at.isoformat()}"
        )
        click.echo(f"{'─' * 70}\n")

        timeline: list[RulingRow] = []
        await _watch_price_ruling_loop(latest_price, rule, position, window_end, timeline)

        for row in timeline:
            await activity.log(
                KIND_PROVISIONAL_RULING,
                {
                    "price": str(row.price),
                    "ruling": row.ruling,
                    "entry_reference": str(entry_reference),
                    "side": side,
                },
            )

        last_ruling = timeline[-1].ruling if timeline else "undecided"

        click.echo(f"\n[{_ts()}] Window closed. Last provisional ruling: {last_ruling.upper()}")
        click.echo(f"[{_ts()}] Polling for official settlement...")

        payout = await _poll_settlement(
            resolved.condition_id,
            resolved.slug,
            fired_token,
            side_of.get(fired_token, "unknown"),
            window_end,
            activity,
        )

        # --- Final report -----------------------------------------------
        click.echo(f"\n{'=' * 70}")
        click.echo("  FINAL COMPARISON REPORT")
        click.echo(f"{'=' * 70}")
        click.echo(f"  Market:            {resolved.slug} ({resolved.condition_id})")
        click.echo(f"  Side bought:       {side}")
        click.echo(f"  Entry reference:   {entry_reference}")
        click.echo(f"  Entry at:          {entry_at.isoformat()}")
        click.echo(f"\n  Ruling timeline ({len(timeline)} samples):")
        for row in timeline:
            click.echo(f"    [{row.ts.isoformat()}] BTC={row.price} ruling={row.ruling.upper()}")
        click.echo(f"\n  Rule's LAST ruling before settlement: {last_ruling.upper()}")

        if payout is None:
            click.echo("  Official settlement:               TIMED OUT / UNKNOWN")
            click.echo("  Comparison:                        CANNOT COMPARE (no settlement data)")
        else:
            official_result = "WON" if payout >= Decimal("0.5") else "LOST"
            click.echo(f"  Official settlement:               {official_result} (payout={payout})")
            if last_ruling.upper() == official_result:
                click.echo("  Comparison:                        AGREED")
            elif last_ruling.upper() == "UNDECIDED":
                click.echo(
                    "  Comparison:                        RULE UNDECIDED at close "
                    f"(official={official_result}) -- not a disagreement, but no "
                    "call was made"
                )
            else:
                click.echo(
                    "  Comparison:                        *** DISAGREED *** "
                    f"(rule said {last_ruling.upper()}, official was {official_result})"
                )
        click.echo(f"{'=' * 70}\n")


@click.command()
@click.option("--bot-id", default="btc-updown-5m-1", show_default=True)
@click.option("--trigger", default="0.30", show_default=True)
@click.option("--size", default="5", show_default=True)
@click.option("--min-window-secs", default=120, show_default=True)
def main(bot_id: str, trigger: str, size: str, min_window_secs: int) -> None:
    """Fire a real live snipe trade and verify the winning rule against live settlement."""
    try:
        asyncio.run(_run(bot_id, Decimal(trigger), Decimal(size), min_window_secs))
    except (KeyboardInterrupt, asyncio.CancelledError):
        click.echo("\nAborted.")


if __name__ == "__main__":
    main()
