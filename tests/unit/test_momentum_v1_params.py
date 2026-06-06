"""Unit tests — MomentumV1 reads strategy params from BotConfig.strategy_params.

Verifies that:
1. ClassVar defaults are used when strategy_params is empty.
2. Per-config values override the ClassVar defaults.
3. Different param sets produce different behaviour in on_tick.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from src.bots.base import BotConfig, BotDeps, RiskCaps, Schedule
from src.bots.momentum.strategy import MomentumV1
from src.exchanges.base import Market, Mode
from src.signals.base import SignalSnapshot
from tests.fixtures.clocks import ManualClock
from tests.fixtures.echo_exchange import EchoExchange
from tests.fixtures.state import (
    InMemoryAuditLogger,
    InMemoryKillSwitch,
    InMemoryStateRepository,
)


def _make_market() -> Market:
    return Market(
        market_id="test-market",
        token_id="token-0",
        tick_size=Decimal("0.01"),
        min_size=Decimal("1"),
        venue="echo",
    )


def _make_bot(params: dict[str, Any]) -> MomentumV1:
    config = BotConfig(
        bot_id="test-bot",
        strategy_name="momentum_v1",
        market_id="test-market",
        mode=Mode.PAPER,
        live=False,
        schedule=Schedule(every_seconds=900),
        risk=RiskCaps(
            max_position_notional=Decimal("1000"),
            max_daily_loss=Decimal("100"),
            max_orders_per_minute=10,
        ),
        signal_subscriptions=["btc_15min"],
        strategy_params=params,
    )
    deps = BotDeps(
        adapter=EchoExchange(),
        state=InMemoryStateRepository(),
        kill_switch=InMemoryKillSwitch(tripped=False),
        heartbeat=_DummyHeartbeat(),
        audit=InMemoryAuditLogger(),
        clock=ManualClock(),
    )
    return MomentumV1(market=_make_market(), config=config, deps=deps)


class _DummyHeartbeat:
    async def beat(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Tests — param loading
# ---------------------------------------------------------------------------


def test_classvar_defaults_when_params_empty() -> None:
    bot = _make_bot({})
    assert bot._momentum_threshold == MomentumV1.MOMENTUM_THRESHOLD_PCT
    assert bot._target_size_cfg == MomentumV1.TARGET_SIZE
    assert bot._max_ticks == MomentumV1.MAX_TICKS_IN_POSITION
    assert bot._buy_price == MomentumV1.BUY_PRICE
    assert bot._sell_price == MomentumV1.SELL_PRICE


def test_config_params_override_classvars() -> None:
    params = {
        "momentum_threshold_pct": "0.3",
        "target_size": "5",
        "max_ticks_in_position": 3,
        "buy_price": "0.35",
        "sell_price": "0.25",
    }
    bot = _make_bot(params)
    assert bot._momentum_threshold == Decimal("0.3")
    assert bot._target_size_cfg == Decimal("5")
    assert bot._max_ticks == 3
    assert bot._buy_price == Decimal("0.35")
    assert bot._sell_price == Decimal("0.25")


def test_partial_params_override_only_specified_keys() -> None:
    bot = _make_bot({"buy_price": "0.40"})
    assert bot._buy_price == Decimal("0.40")
    # Everything else stays at ClassVar default
    assert bot._momentum_threshold == MomentumV1.MOMENTUM_THRESHOLD_PCT
    assert bot._sell_price == MomentumV1.SELL_PRICE


# ---------------------------------------------------------------------------
# Tests — on_tick uses instance vars, not ClassVars
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_tick_uses_config_buy_price() -> None:
    """With a very low threshold, a small momentum triggers a BUY at config price."""
    from src.signals.base import SignalSnapshot

    params = {
        "momentum_threshold_pct": "0.01",  # tiny threshold — any up-tick triggers
        "buy_price": "0.40",
        "target_size": "3",
    }
    bot = _make_bot(params)

    def _snap(price: str) -> SignalSnapshot:
        return SignalSnapshot(samples={"btc_15min": {"price_usd": price}}, stale=set(), received_at=datetime.now(tz=UTC))

    # First tick — just records price
    await bot.on_tick(_snap("100000.00"))

    # Second tick — price went up 1% → should BUY
    decision = await bot.on_tick(_snap("101000.00"))

    assert len(decision.intents) == 1
    assert decision.intents[0].price == Decimal("0.40")
    assert decision.intents[0].size == Decimal("3")


@pytest.mark.asyncio
async def test_on_tick_uses_config_momentum_threshold() -> None:
    """With a very high threshold, a small move should NOT trigger a BUY."""
    from src.signals.base import SignalSnapshot

    params = {"momentum_threshold_pct": "10.0"}  # requires 10% BTC move
    bot = _make_bot(params)

    def _snap(price: str) -> SignalSnapshot:
        return SignalSnapshot(samples={"btc_15min": {"price_usd": price}}, stale=set(), received_at=datetime.now(tz=UTC))

    await bot.on_tick(_snap("100000.00"))
    decision = await bot.on_tick(_snap("100500.00"))  # only 0.5% — below threshold

    assert decision.intents == []
    assert "first_tick" not in decision.note  # not the first tick
