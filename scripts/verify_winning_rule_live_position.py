"""Evaluate the ``price_compare`` winning rule against an ALREADY-open live
position (bought via a separate ``snipe_bot.py`` run), and compare its final
ruling to official settlement.

This is the companion to ``verify_winning_rule.py`` for the case where the
live trade was already fired by a separate process and we just want to
independently watch the winning rule against it -- no new order is placed
here.

Usage:
    uv run python scripts/verify_winning_rule_live_position.py \\
        --condition-id 0x621dd6cfd580a292713f242869fb848a353c1f10868e7c7b21c4bdd32f57929d \\
        --token-id 49770272484104615354750161142430441833669981715329321362389127989333539144715 \\
        --side UP --window-end 1784000700.0
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

from scripts.snipe_bot import _check_clob_settlement, _ts  # noqa: E402
from scripts.verify_winning_rule import _evaluate_rule, _fetch_btc_price  # noqa: E402


@dataclass
class RulingRow:
    ts: datetime
    price: Decimal
    ruling: str


async def _run(
    condition_id: str, token_id: str, side: str, window_end: float, size: Decimal
) -> None:
    from src.rules.base import PositionState
    from src.rules.registry import rules as rule_registry

    rule_registry.autodiscover()
    rule_cls = rule_registry.get("polymarket.btc_up_or_down_5m.price_compare")
    rule = rule_cls()

    entry_result = await _fetch_btc_price()
    if entry_result is None:
        click.echo("ERROR: could not fetch entry-reference BTC price.", err=True)
        sys.exit(1)
    entry_price, entry_source = entry_result
    entry_at = datetime.now(tz=UTC)
    click.echo(
        f"[{_ts()}] Entry reference captured: BTC={entry_price} (source={entry_source})"
    )
    click.echo(
        "  NOTE: this is captured now, slightly after the real fire time -- treat as an "
        "approximation of the true entry price, not exact.\n"
    )

    position = PositionState(
        market_id=condition_id,
        side=side,
        entry_reference=entry_price,
        entry_at=entry_at,
        size=size,
    )

    timeline: list[RulingRow] = []
    secs_left = int(window_end - time.time())
    click.echo(f"  Watching until window close ({secs_left}s left)...\n")

    while time.time() < window_end:
        result = await _fetch_btc_price()
        if result is not None:
            price, source = result
            ruling = _evaluate_rule(rule, position, price, source)
            timeline.append(RulingRow(ts=datetime.now(tz=UTC), price=price, ruling=ruling))
            click.echo(
                f"[{_ts()}] BTC={price}  ruling={ruling.upper()}  "
                f"(entry={entry_price}, side={side})"
            )
        remaining = window_end - time.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(12.0, max(0.5, remaining)))

    click.echo(f"\n[{_ts()}] Window closed. Polling for official settlement...")
    deadline = window_end + 900
    payout: Decimal | None = None
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            payout = _check_clob_settlement(condition_id, token_id)
            if payout is not None:
                break
            click.echo(f"[{_ts()}] Not yet settled (attempt {attempts}) -- retrying in 30s...")
        except Exception as exc:
            click.echo(f"[{_ts()}] Settlement check error: {exc} -- retrying in 30s...")
        await asyncio.sleep(30)

    click.echo(f"\n{'=' * 70}")
    if payout is None:
        click.echo("  Settlement poll timed out -- no comparison possible.")
    else:
        official_result = "WIN" if payout >= Decimal("0.5") else "LOSS"
        click.echo(f"  Official settlement: {official_result} (payout={payout})")
        last_ruling = timeline[-1].ruling if timeline else "undecided"
        official_ruling = "won" if official_result == "WIN" else "lost"
        if last_ruling.lower() == official_ruling:
            click.echo("  Comparison:                        AGREED")
        elif last_ruling.upper() == "UNDECIDED":
            click.echo(
                "  Comparison:                        RULE UNDECIDED at close "
                f"(official={official_result}) -- not a disagreement, but no call was made"
            )
        else:
            click.echo(
                "  Comparison:                        *** DISAGREED *** "
                f"(rule said {last_ruling.upper()}, official was {official_result})"
            )
    click.echo(f"{'=' * 70}\n")


@click.command()
@click.option("--condition-id", required=True)
@click.option("--token-id", required=True, help="Token id of the side we bought")
@click.option("--side", required=True, type=click.Choice(["UP", "DOWN"], case_sensitive=False))
@click.option("--window-end", required=True, type=float, help="Unix timestamp window closes")
@click.option("--size", default="5", show_default=True)
def main(condition_id: str, token_id: str, side: str, window_end: float, size: str) -> None:
    try:
        asyncio.run(_run(condition_id, token_id, side.upper(), window_end, Decimal(size)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        click.echo("\nAborted.")


if __name__ == "__main__":
    main()
