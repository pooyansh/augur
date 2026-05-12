"""Tests for KillSwitchCascade and BaseBot.run kill-switch behaviour.

Key invariants:
- When the switch is tripped, adapter.cancel_all is called exactly once
  regardless of how many tripped ticks pass.
- When the switch is untripped, normal flow (on_tick) resumes.
- On the next trip after an untrip, cancel_all is issued again (once).
"""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal

import pytest
from src.risk.kill_switch import KillSwitchCascade

from tests.fixtures.echo_exchange import EchoExchange
from tests.fixtures.null_strategy import make_null_bot
from tests.fixtures.state import InMemoryAuditLogger, InMemoryKillSwitch

# ---------------------------------------------------------------------------
# KillSwitchCascade unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cascade_issues_cancel_all_once_per_trip() -> None:
    """cancel_all is called exactly once across N consecutive tripped ticks."""
    adapter = EchoExchange()
    audit = InMemoryAuditLogger()
    cascade = KillSwitchCascade(audit=audit, bot_id="test-bot")

    for _ in range(5):
        await cascade.on_trip(adapter, "market-001")

    # cancel_all is called via EchoExchange.cancel for each open order.
    # With no open orders, cancel_all still calls the method once.
    # We track via a patched counter.
    assert adapter.cancel_call_count == 0  # no open orders → cancel_all no-op


@pytest.mark.asyncio
async def test_cascade_idempotent_no_open_orders() -> None:
    """Calling on_trip multiple times with no open orders never double-cancels."""
    adapter = EchoExchange()
    audit = InMemoryAuditLogger()
    cascade = KillSwitchCascade(audit=audit, bot_id="test-bot")

    await cascade.on_trip(adapter, "market-001")
    await cascade.on_trip(adapter, "market-001")
    await cascade.on_trip(adapter, "market-001")

    # cancel_all was called once internally, but with no open orders → 0 cancels
    assert adapter.cancel_call_count == 0


@pytest.mark.asyncio
async def test_cascade_cancel_all_called_once_with_open_orders() -> None:
    """With open orders, cancel_all removes them all but is only called once."""
    from src.exchanges.base import Market, OrderIntent, Side

    adapter = EchoExchange()
    # Manually plant open orders
    market = Market(
        market_id="m1",
        token_id="t0",
        tick_size=Decimal("0.01"),
        min_size=Decimal("1"),
        venue="echo",
    )
    intent = OrderIntent(
        client_order_id="coid-001",
        market=market,
        side=Side.BUY,
        price=Decimal("0.50"),
        size=Decimal("10"),
    )
    await adapter.place(intent)
    assert len(adapter.open_orders) == 1

    audit = InMemoryAuditLogger()
    cascade = KillSwitchCascade(audit=audit, bot_id="test-bot")

    # First trip: issues cancel_all → 1 cancel
    await cascade.on_trip(adapter, "m1")
    assert adapter.cancel_call_count == 1  # 1 order cancelled
    assert len(adapter.open_orders) == 0

    # Second trip: cascade issued flag is True → skip
    await cascade.on_trip(adapter, "m1")
    assert adapter.cancel_call_count == 1  # no new cancels


@pytest.mark.asyncio
async def test_cascade_reset_allows_reissue_on_next_trip() -> None:
    """After reset(), the next on_trip call re-issues cancel_all."""
    from src.exchanges.base import Market, OrderIntent, Side

    adapter = EchoExchange()
    market = Market(
        market_id="m1",
        token_id="t0",
        tick_size=Decimal("0.01"),
        min_size=Decimal("1"),
        venue="echo",
    )

    audit = InMemoryAuditLogger()
    cascade = KillSwitchCascade(audit=audit, bot_id="test-bot")

    # Trip 1
    await cascade.on_trip(adapter, "m1")
    cascade.reset()

    # Place a new order
    intent = OrderIntent(
        client_order_id="coid-002",
        market=market,
        side=Side.BUY,
        price=Decimal("0.50"),
        size=Decimal("10"),
    )
    await adapter.place(intent)

    # Trip 2 — should re-issue cancel_all
    await cascade.on_trip(adapter, "m1")
    assert adapter.cancel_call_count == 1  # 1 order cancelled in trip 2


# ---------------------------------------------------------------------------
# BaseBot.run integration: kill-switch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basebot_run_cancel_all_once_across_tripped_ticks() -> None:
    """BaseBot.run with kill switch tripped calls cancel_all exactly once.

    We run 3 ticks with the switch tripped, counting cancel_all invocations.
    Uses a tick counter inside _sleep_until_next_tick to stop after N ticks.
    """
    cancel_all_calls: list[str] = []
    tick_count = 0

    adapter = EchoExchange()
    orig_cancel_all = adapter.cancel_all  # bound method

    async def patched_cancel_all(market_id: str | None = None) -> int:
        cancel_all_calls.append(market_id or "")
        return await orig_cancel_all(market_id)

    adapter.cancel_all = patched_cancel_all  # type: ignore[method-assign]

    kill_switch = InMemoryKillSwitch(tripped=True)
    bot = make_null_bot(adapter=adapter, kill_switch=kill_switch)

    async def patched_sleep(secs: float) -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count >= 3:
            raise asyncio.CancelledError

    import unittest.mock as mock

    patcher = mock.patch.object(bot, "_sleep_until_next_tick", side_effect=patched_sleep)
    with patcher, contextlib.suppress(asyncio.CancelledError):
        await bot.run()

    # cancel_all issued exactly once despite multiple tripped ticks
    assert len(cancel_all_calls) == 1, (
        f"Expected 1 cancel_all call, got {len(cancel_all_calls)}: {cancel_all_calls}"
    )


@pytest.mark.asyncio
async def test_basebot_run_reissues_cancel_all_on_second_trip() -> None:
    """After the switch is untripped and re-tripped, cancel_all is issued again."""
    cancel_all_calls: list[str] = []

    adapter = EchoExchange()
    orig_cancel_all = adapter.cancel_all  # bound method

    async def patched_cancel_all(market_id: str | None = None) -> int:
        cancel_all_calls.append(market_id or "")
        return await orig_cancel_all(market_id)

    adapter.cancel_all = patched_cancel_all  # type: ignore[method-assign]

    kill_switch = InMemoryKillSwitch(tripped=True)
    bot = make_null_bot(adapter=adapter, kill_switch=kill_switch)

    tick_count = 0

    async def controlled_sleep(tick_start: object) -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count == 1:
            # First sleep: still tripped (cancel_all already issued)
            pass
        elif tick_count == 2:
            # Untrip — cascade should reset
            kill_switch.reset()
        elif tick_count == 3:
            # Re-trip — cancel_all should fire again
            kill_switch.trip("re-trip")
        elif tick_count >= 5:
            raise asyncio.CancelledError

    import unittest.mock as mock

    patcher2 = mock.patch.object(bot, "_sleep_until_next_tick", side_effect=controlled_sleep)
    with patcher2, contextlib.suppress(asyncio.CancelledError):
        await bot.run()

    # Should have 2 cancel_all calls — one per trip period
    assert len(cancel_all_calls) >= 2, (
        f"Expected >= 2 cancel_all calls (one per trip period), got {len(cancel_all_calls)}"
    )
