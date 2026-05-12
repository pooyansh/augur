"""Integration test: kill-switch cascade across two paper bots.

Two NullStrategy bots run against EchoAdapter instances.  When the global
kill switch is tripped, both bots' adapters should receive ``cancel_all`` and
both should refuse new ``place()`` calls.
"""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from typing import Any

import pytest
from src.exchanges.base import Market, OrderIntent, Side

from tests.fixtures.echo_exchange import EchoExchange
from tests.fixtures.null_strategy import make_null_bot
from tests.fixtures.state import InMemoryKillSwitch

pytestmark = pytest.mark.integration


def _make_market(market_id: str = "m1") -> Market:
    return Market(
        market_id=market_id,
        token_id="t0",
        tick_size=Decimal("0.01"),
        min_size=Decimal("1"),
        venue="echo",
    )


@pytest.mark.asyncio
async def test_both_bots_receive_cancel_all_on_kill_switch_trip() -> None:
    """Trip the kill switch; both bots' adapters must receive cancel_all.

    Uses a shared InMemoryKillSwitch so both bots see the same state.
    """
    kill_switch = InMemoryKillSwitch(tripped=False)
    adapter_a = EchoExchange()
    adapter_b = EchoExchange()

    bot_a = make_null_bot(bot_id="bot-a", adapter=adapter_a, kill_switch=kill_switch)
    bot_b = make_null_bot(bot_id="bot-b", adapter=adapter_b, kill_switch=kill_switch)

    # Place orders on both bots' adapters
    market_a = _make_market("m-a")
    market_b = _make_market("m-b")

    intent_a = OrderIntent(
        client_order_id="coid-a-1",
        market=market_a,
        side=Side.BUY,
        price=Decimal("0.50"),
        size=Decimal("10"),
    )
    intent_b = OrderIntent(
        client_order_id="coid-b-1",
        market=market_b,
        side=Side.BUY,
        price=Decimal("0.60"),
        size=Decimal("5"),
    )
    await adapter_a.place(intent_a)
    await adapter_b.place(intent_b)

    assert len(adapter_a.open_orders) == 1
    assert len(adapter_b.open_orders) == 1

    # Track cancel_all calls
    cancel_all_a: list[Any] = []
    cancel_all_b: list[Any] = []
    orig = EchoExchange.cancel_all

    async def patched_a(self: EchoExchange, market_id: str | None = None) -> int:  # type: ignore[override]
        cancel_all_a.append(market_id)
        return await orig(self, market_id)

    async def patched_b(self: EchoExchange, market_id: str | None = None) -> int:  # type: ignore[override]
        cancel_all_b.append(market_id)
        return await orig(self, market_id)

    adapter_a.cancel_all = patched_a.__get__(adapter_a, EchoExchange)  # type: ignore[method-assign]
    adapter_b.cancel_all = patched_b.__get__(adapter_b, EchoExchange)  # type: ignore[method-assign]

    # Trip the switch
    kill_switch.trip("integration test")

    # Run both bots concurrently for a short window, then cancel
    import unittest.mock as mock

    tick_count = {"a": 0, "b": 0}

    async def sleep_a(secs: float) -> None:
        tick_count["a"] += 1
        if tick_count["a"] >= 2:
            raise asyncio.CancelledError

    async def sleep_b(secs: float) -> None:
        tick_count["b"] += 1
        if tick_count["b"] >= 2:
            raise asyncio.CancelledError

    with mock.patch("asyncio.sleep", side_effect=sleep_a):
        task_a = asyncio.create_task(bot_a.run())
        with contextlib.suppress(asyncio.CancelledError):
            await task_a

    # Reset mock for bot_b
    with mock.patch("asyncio.sleep", side_effect=sleep_b):
        task_b = asyncio.create_task(bot_b.run())
        with contextlib.suppress(asyncio.CancelledError):
            await task_b

    assert len(cancel_all_a) == 1, "Bot A adapter must receive exactly one cancel_all"
    assert len(cancel_all_b) == 1, "Bot B adapter must receive exactly one cancel_all"
    # Both open orders should be gone
    assert len(adapter_a.open_orders) == 0
    assert len(adapter_b.open_orders) == 0


@pytest.mark.asyncio
async def test_both_bots_refuse_place_when_kill_switch_tripped() -> None:
    """After trip, place() on both bots raises KillSwitchTripped."""
    from src.bots.base import KillSwitchTripped
    from src.exchanges.base import OrderTemplate

    kill_switch = InMemoryKillSwitch(tripped=True)
    adapter_a = EchoExchange()
    adapter_b = EchoExchange()

    bot_a = make_null_bot(bot_id="bot-a", adapter=adapter_a, kill_switch=kill_switch)
    bot_b = make_null_bot(bot_id="bot-b", adapter=adapter_b, kill_switch=kill_switch)

    market = _make_market()
    template = OrderTemplate(
        market=market,
        side=Side.BUY,
        price=Decimal("0.50"),
        size=Decimal("1"),
    )

    with pytest.raises(KillSwitchTripped):
        await bot_a.place(template)

    with pytest.raises(KillSwitchTripped):
        await bot_b.place(template)

    # Adapters must not have been called
    assert adapter_a.place_call_count == 0
    assert adapter_b.place_call_count == 0
