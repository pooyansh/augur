"""Tests for momentum_v1 strategy — unit tests, backtest harness, Hypothesis.

Three sections:
1. Unit tests: deterministic on_tick scenarios.
2. Backtest harness: replay a BTC price sequence through on_tick.
3. Hypothesis: threshold invariant and snapshot/rehydrate round-trip.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from src.bots.base import BotConfig, BotDeps, Decision, LocalHeartbeat, Schedule
from src.bots.momentum.strategy import MomentumV1
from src.exchanges.base import Market, Mode, Side
from src.risk.caps import RiskCaps
from src.signals.base import SignalSnapshot

from tests.fixtures.clocks import ManualClock
from tests.fixtures.echo_exchange import EchoExchange
from tests.fixtures.state import (
    InMemoryAuditLogger,
    InMemoryKillSwitch,
    InMemoryStateRepository,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _market(tick_size: str = "0.01", min_size: str = "1") -> Market:
    return Market(
        market_id="0xABCDEF0001",
        token_id="71321045",
        tick_size=Decimal(tick_size),
        min_size=Decimal(min_size),
        venue="polymarket",
    )


def _make_bot(
    market: Market | None = None,
    clock: ManualClock | None = None,
    *,
    reject: bool = False,
) -> MomentumV1:
    market = market or _market()
    clock = clock or ManualClock()
    config = BotConfig(
        bot_id="test-momentum-v1",
        strategy_name="momentum_v1",
        market_id=market.market_id,
        mode=Mode.PAPER,
        live=False,
        schedule=Schedule(every_seconds=900),
        risk=RiskCaps(
            max_position_notional=Decimal("500"),
            max_daily_loss=Decimal("50"),
            max_orders_per_minute=10,
        ),
        signal_subscriptions=["btc_15min"],
    )
    deps = BotDeps(
        adapter=EchoExchange(reject=reject),
        state=InMemoryStateRepository(),
        kill_switch=InMemoryKillSwitch(),
        heartbeat=LocalHeartbeat(clock),
        audit=InMemoryAuditLogger(),
        clock=clock,
    )
    return MomentumV1(market, config, deps)


def _snap(price_usd: str | Decimal, *, stale: bool = False) -> SignalSnapshot:
    stale_set: frozenset[str] = frozenset(["btc_15min"]) if stale else frozenset()
    return SignalSnapshot(
        samples={
            "btc_15min": {
                "price_usd": str(price_usd),
                "source": "test",
                "source_ts": "2026-05-12T00:00:00+00:00",
            }
        },
        received_at=datetime.now(tz=UTC),
        stale=stale_set,
    )


def _empty_snap() -> SignalSnapshot:
    return SignalSnapshot(samples={}, received_at=datetime.now(tz=UTC), stale=frozenset())


# ---------------------------------------------------------------------------
# 1. Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_tick_is_noop() -> None:
    """First tick with no prior price must produce no order."""
    bot = _make_bot()
    decision = await bot.on_tick(_snap("67000"))
    assert decision.intents == []
    assert decision.note == "first_tick"


@pytest.mark.asyncio
async def test_second_tick_no_momentum_no_order() -> None:
    """Price unchanged → no entry."""
    bot = _make_bot()
    await bot.on_tick(_snap("67000"))
    decision = await bot.on_tick(_snap("67000"))
    assert decision.intents == []


@pytest.mark.asyncio
async def test_momentum_below_threshold_no_order() -> None:
    """Price up by < threshold → no entry."""
    bot = _make_bot()
    await bot.on_tick(_snap("67000"))
    # 0.3% move is below 0.5% threshold
    decision = await bot.on_tick(_snap("67201"))  # +0.30%
    assert decision.intents == []


@pytest.mark.asyncio
async def test_entry_on_sufficient_momentum() -> None:
    """Price up by ≥ threshold → BUY YES."""
    bot = _make_bot()
    await bot.on_tick(_snap("67000"))
    # 0.6% move is above 0.5% threshold
    decision = await bot.on_tick(_snap("67402"))  # +0.60%
    assert len(decision.intents) == 1
    intent = decision.intents[0]
    assert intent.side == Side.BUY
    assert intent.price == MomentumV1.BUY_PRICE
    assert intent.size == MomentumV1.TARGET_SIZE


@pytest.mark.asyncio
async def test_hold_while_in_position() -> None:
    """After entry, ticks below max do not generate new orders."""
    bot = _make_bot()
    await bot.on_tick(_snap("67000"))
    await bot.on_tick(_snap("67402"))  # entry tick
    decision = await bot.on_tick(_snap("67500"))  # hold tick
    assert decision.intents == []
    assert "hold" in decision.note


@pytest.mark.asyncio
async def test_exit_after_max_ticks() -> None:
    """Position is exited after MAX_TICKS_IN_POSITION held ticks."""
    bot = _make_bot()
    max_ticks = MomentumV1.MAX_TICKS_IN_POSITION

    await bot.on_tick(_snap("67000"))
    await bot.on_tick(_snap("67402"))  # entry

    # Hold for max_ticks - 1 ticks (no exit yet)
    for _ in range(max_ticks - 1):
        decision = await bot.on_tick(_snap("67500"))
        assert decision.intents == []

    # Final tick triggers timeout exit
    decision = await bot.on_tick(_snap("67500"))
    assert len(decision.intents) == 1
    intent = decision.intents[0]
    assert intent.side == Side.SELL
    assert "timeout" in decision.note


@pytest.mark.asyncio
async def test_early_exit_on_reversal() -> None:
    """Momentum reversal while in position triggers early exit."""
    bot = _make_bot()
    await bot.on_tick(_snap("67000"))
    await bot.on_tick(_snap("67402"))  # entry

    # Price drops > 0.5%
    decision = await bot.on_tick(_snap("67000"))  # back to start → ~-0.6%
    assert len(decision.intents) == 1
    assert decision.intents[0].side == Side.SELL
    assert "reversal" in decision.note


@pytest.mark.asyncio
async def test_stale_signal_skips_tick() -> None:
    """Stale signal produces no order and marks the note."""
    bot = _make_bot()
    decision = await bot.on_tick(_snap("67000", stale=True))
    assert decision.intents == []
    assert decision.note == "btc_15min_stale"


@pytest.mark.asyncio
async def test_absent_signal_skips_tick() -> None:
    """Missing signal key produces no order."""
    bot = _make_bot()
    decision = await bot.on_tick(_empty_snap())
    assert decision.intents == []
    assert decision.note == "btc_15min_absent"


@pytest.mark.asyncio
async def test_min_size_respected() -> None:
    """Entry size is at least market.min_size even when TARGET_SIZE is smaller."""
    market = _market(min_size="20")
    bot = _make_bot(market)
    await bot.on_tick(_snap("67000"))
    decision = await bot.on_tick(_snap("67402"))
    assert len(decision.intents) == 1
    assert decision.intents[0].size == Decimal("20")


# ---------------------------------------------------------------------------
# 2. Backtest harness
# ---------------------------------------------------------------------------


class BacktestResult:
    """Summary of a backtest run."""

    def __init__(self) -> None:
        self.decisions: list[tuple[Decimal, str]] = []  # (btc_price, note)
        self.entries: int = 0
        self.exits: int = 0

    def record(self, price: Decimal, decision: Decision) -> None:
        self.decisions.append((price, decision.note))
        for intent in decision.intents:
            if intent.side == Side.BUY:
                self.entries += 1
            elif intent.side == Side.SELL:
                self.exits += 1


def run_backtest(prices: Sequence[Decimal]) -> BacktestResult:
    """Replay a BTC price sequence through MomentumV1.on_tick.

    Args:
        prices: Sequence of BTC/USD prices, one per tick.

    Returns:
        BacktestResult with entry/exit counts and per-tick notes.
    """
    bot = _make_bot()
    result = BacktestResult()

    async def _run() -> None:
        for price in prices:
            decision = await bot.on_tick(_snap(price))
            result.record(price, decision)

    asyncio.get_event_loop().run_until_complete(_run())
    return result


def test_backtest_trending_up_enters() -> None:
    """A monotonically rising BTC sequence should generate at least one entry.

    Each step is ~0.6% (above the 0.5% threshold) so every tick from the
    second onward qualifies as a momentum entry signal.
    """
    # 67000 * 1.006^n — each step is ~0.6%, comfortably above the 0.5% threshold.
    base = Decimal("67000")
    factor = Decimal("1.006")
    prices = [base * (factor**i) for i in range(10)]
    result = run_backtest(prices)
    assert result.entries >= 1


def test_backtest_flat_market_no_entries() -> None:
    """Flat price sequence generates no entries."""
    prices = [Decimal("67000")] * 10
    result = run_backtest(prices)
    assert result.entries == 0


def test_backtest_entries_leq_exits_plus_one() -> None:
    """Entries and exits stay balanced: exits ≤ entries ≤ exits + 1."""
    prices = [
        Decimal("67000"),
        Decimal("67402"),  # entry (+0.6%)
        Decimal("67500"),  # hold
        Decimal("67000"),  # reversal → exit
        Decimal("67402"),  # re-entry
        Decimal("67500"),
        Decimal("67000"),  # exit
    ]
    result = run_backtest(prices)
    assert result.exits <= result.entries <= result.exits + 1


def test_backtest_snapshot_rehydrate_midway() -> None:
    """Snapshot → rehydrate mid-backtest resumes state correctly."""
    bot = _make_bot()
    clock = ManualClock()
    bot._deps = BotDeps(
        adapter=EchoExchange(),
        state=InMemoryStateRepository(),
        kill_switch=InMemoryKillSwitch(),
        heartbeat=LocalHeartbeat(clock),
        audit=InMemoryAuditLogger(),
        clock=clock,
    )

    async def _run() -> None:
        await bot.on_tick(_snap("67000"))
        await bot.on_tick(_snap("67402"))  # enter

        snap = bot.snapshot()
        assert snap["prev_btc_price"] == "67402"
        assert Decimal(snap["position_size"]) == MomentumV1.TARGET_SIZE

        # Rehydrate fresh bot
        bot2 = _make_bot(clock=clock)
        bot2.rehydrate(snap)
        assert bot2._prev_btc_price == Decimal("67402")
        assert bot2._position_size == MomentumV1.TARGET_SIZE

        # Continue from rehydrated state
        decision = await bot2.on_tick(_snap("67500"))  # hold
        assert decision.intents == []

    asyncio.get_event_loop().run_until_complete(_run())


# ---------------------------------------------------------------------------
# 3. Hypothesis
# ---------------------------------------------------------------------------


@given(
    prev=st.decimals(min_value=Decimal("1000"), max_value=Decimal("200000"), places=2),
    pct_change=st.decimals(min_value=Decimal("-0.49"), max_value=Decimal("0.49"), places=2),
)
@settings(max_examples=200)
def test_below_threshold_never_enters(prev: Decimal, pct_change: Decimal) -> None:
    """Price move strictly inside (-threshold, +threshold) → no entry ever."""
    current = prev * (1 + pct_change / 100)

    async def _run() -> None:
        bot = _make_bot()
        await bot.on_tick(_snap(prev))
        decision = await bot.on_tick(_snap(current))
        assert decision.intents == []

    asyncio.get_event_loop().run_until_complete(_run())


@given(
    prev=st.decimals(min_value=Decimal("1000"), max_value=Decimal("200000"), places=2),
    pct_above=st.decimals(min_value=Decimal("0.50"), max_value=Decimal("10"), places=2),
)
@settings(max_examples=200)
def test_above_threshold_enters(prev: Decimal, pct_above: Decimal) -> None:
    """Price move ≥ threshold always triggers a BUY."""
    current = prev * (1 + pct_above / 100)

    async def _run() -> None:
        bot = _make_bot()
        await bot.on_tick(_snap(prev))
        decision = await bot.on_tick(_snap(current))
        assert len(decision.intents) == 1
        assert decision.intents[0].side == Side.BUY

    asyncio.get_event_loop().run_until_complete(_run())


_dec_pos = st.decimals(min_value=Decimal("0"), max_value=Decimal("1000"), places=2).map(str)
_dec_btc = st.decimals(
    min_value=Decimal("1000"), max_value=Decimal("200000"), places=2
).map(str)
_dec_size = st.decimals(
    min_value=Decimal("0"), max_value=Decimal("100"), places=2
).map(str)


@given(
    snap_dict=st.fixed_dictionaries(
        {
            "intent_seq": st.integers(min_value=0, max_value=10_000),
            "position": _dec_pos,
            "last_decision_at": st.just("2026-05-12T00:00:00+00:00"),
            "prev_btc_price": st.one_of(st.none(), _dec_btc),
            "position_size": _dec_size,
            "ticks_in_position": st.integers(min_value=0, max_value=100),
        }
    )
)
def test_rehydrate_roundtrip(snap_dict: dict) -> None:  # type: ignore[type-arg]
    """snapshot → rehydrate → snapshot produces identical data."""
    bot = _make_bot()
    bot.rehydrate(snap_dict)
    snap2 = bot.snapshot()

    assert int(snap2["intent_seq"]) == snap_dict["intent_seq"]

    if snap_dict["prev_btc_price"] is None:
        assert snap2["prev_btc_price"] is None
    else:
        assert Decimal(snap2["prev_btc_price"]) == Decimal(snap_dict["prev_btc_price"])

    assert Decimal(snap2["position_size"]) == Decimal(snap_dict["position_size"])
    assert int(snap2["ticks_in_position"]) == snap_dict["ticks_in_position"]
