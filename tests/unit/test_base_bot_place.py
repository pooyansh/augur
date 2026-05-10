"""Hypothesis property tests for BaseBot.place().

Three properties tested:
1. Idempotency — same client_order_id retried N times produces exactly one
   accepted audit entry (dedup via _inflight cache).
2. Risk caps — intents that exceed any cap always raise RiskCapExceeded and
   never reach the adapter.
3. Kill switch — when tripped, place() raises KillSwitchTripped and zero
   adapter calls are made.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from src.bots.base import KillSwitchTripped, RiskCapExceeded
from src.exchanges.base import Market, OrderIntent, OrderTemplate, Side

from tests.fixtures.echo_exchange import EchoExchange
from tests.fixtures.null_strategy import make_null_bot
from tests.fixtures.state import InMemoryAuditLogger, InMemoryKillSwitch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_market(market_id: str = "market-001") -> Market:
    return Market(
        market_id=market_id,
        token_id="token-0",
        tick_size=Decimal("0.01"),
        min_size=Decimal("1"),
        venue="echo",
    )


# Hypothesis strategy for valid Decimal prices (0.01 to 0.99)
decimal_price = st.decimals(
    min_value="0.01",
    max_value="0.99",
    places=2,
    allow_nan=False,
    allow_infinity=False,
)

# Hypothesis strategy for valid sizes (1 to 100)
decimal_size = st.decimals(
    min_value="1",
    max_value="100",
    places=0,
    allow_nan=False,
    allow_infinity=False,
)

# Retry counts: 2 to 5 retries
retry_count = st.integers(min_value=2, max_value=5)


# ---------------------------------------------------------------------------
# Property 1: Idempotency under retry
# ---------------------------------------------------------------------------


@given(price=decimal_price, size=decimal_size, n_retries=retry_count)
@settings(max_examples=50)
def test_place_idempotent_under_retry(price: Decimal, size: Decimal, n_retries: int) -> None:
    """Retrying place() with the same client_order_id must yield exactly one
    accepted audit record regardless of retry count.

    The dedup short-circuits via BaseBot._inflight: the first successful call
    populates the cache; subsequent calls return the cached result without
    calling the adapter again.
    """
    audit = InMemoryAuditLogger()
    adapter = EchoExchange()
    bot = make_null_bot(adapter=adapter, audit=audit)

    market = _make_market()
    # Generate one deterministic client_order_id
    coid = bot._next_client_order_id()
    intent = OrderIntent(
        client_order_id=coid,
        market=market,
        side=Side.BUY,
        price=price,
        size=size,
    )

    # Place the same intent n_retries times
    for _ in range(n_retries):
        result = asyncio.get_event_loop().run_until_complete(bot.place(intent))
        assert result.accepted, "EchoExchange must accept"

    # Audit log should have exactly ONE "order_submitted" and ONE "order_accepted"
    # because the dedup cache short-circuits after the first call.
    submitted = audit.submitted_rows()
    accepted = audit.accepted_rows()
    assert len(submitted) == 1, (
        f"Expected 1 submitted audit entry, got {len(submitted)} after {n_retries} retries"
    )
    assert len(accepted) == 1, (
        f"Expected 1 accepted audit entry, got {len(accepted)} after {n_retries} retries"
    )

    # Adapter must also have been called exactly once
    assert adapter.place_call_count == 1, (
        f"Adapter place() called {adapter.place_call_count} times; expected 1"
    )


# ---------------------------------------------------------------------------
# Property 2: Risk caps enforced — intent always blocked, adapter never called
# ---------------------------------------------------------------------------


_large_size = st.decimals(
    min_value="1001", max_value="9999", places=0, allow_nan=False, allow_infinity=False
)


@given(price=decimal_price, size=_large_size)
@settings(max_examples=50)
def test_place_blocks_on_position_cap_exceeded(price: Decimal, size: Decimal) -> None:
    """Intents whose notional (price * size) exceeds the position cap must
    raise RiskCapExceeded and must NOT reach the adapter.

    We set max_position_notional=5.  Any (price >= 0.01) * (size >= 1001)
    gives notional >= 10.01 which always exceeds the 5 cap.
    """
    adapter = EchoExchange()
    bot = make_null_bot(adapter=adapter, max_position_notional=Decimal("5"))

    market = _make_market()
    template = OrderTemplate(market=market, side=Side.BUY, price=price, size=size)

    with pytest.raises(RiskCapExceeded):
        asyncio.get_event_loop().run_until_complete(bot.place(template))

    assert adapter.place_call_count == 0, "Adapter must not be called when cap exceeded"


@given(
    price=decimal_price,
    size=decimal_size,
)
@settings(max_examples=50)
def test_place_blocks_when_daily_loss_cap_reached(price: Decimal, size: Decimal) -> None:
    """When daily loss equals the cap, any new order must be rejected."""
    adapter = EchoExchange()
    bot = make_null_bot(adapter=adapter, max_daily_loss=Decimal("0"))
    # Pre-set daily loss to the cap value.
    bot._daily_loss = Decimal("0")

    market = _make_market()
    template = OrderTemplate(market=market, side=Side.BUY, price=price, size=size)

    with pytest.raises(RiskCapExceeded):
        asyncio.get_event_loop().run_until_complete(bot.place(template))

    assert adapter.place_call_count == 0


@given(price=decimal_price, size=decimal_size)
@settings(max_examples=30)
def test_place_blocks_when_order_rate_exceeded(price: Decimal, size: Decimal) -> None:
    """When max_orders_per_minute=0, every placement attempt must be rejected."""
    adapter = EchoExchange()
    bot = make_null_bot(adapter=adapter, max_orders_per_minute=0)

    market = _make_market()
    template = OrderTemplate(market=market, side=Side.BUY, price=price, size=size)

    with pytest.raises(RiskCapExceeded):
        asyncio.get_event_loop().run_until_complete(bot.place(template))

    assert adapter.place_call_count == 0


# ---------------------------------------------------------------------------
# Property 3: Kill switch — zero adapter calls when tripped
# ---------------------------------------------------------------------------


@given(price=decimal_price, size=decimal_size)
@settings(max_examples=50)
def test_place_raises_when_kill_switch_tripped(price: Decimal, size: Decimal) -> None:
    """When the kill switch is active, place() must raise KillSwitchTripped
    and must not forward the intent to the adapter.

    This is the primary safety invariant: a tripped kill switch is absolute.
    """
    adapter = EchoExchange()
    kill_switch = InMemoryKillSwitch()
    kill_switch.trip("test")
    bot = make_null_bot(adapter=adapter, kill_switch=kill_switch)

    market = _make_market()
    template = OrderTemplate(market=market, side=Side.BUY, price=price, size=size)

    with pytest.raises(KillSwitchTripped):
        asyncio.get_event_loop().run_until_complete(bot.place(template))

    assert adapter.place_call_count == 0, (
        "Adapter must receive zero calls when kill switch is tripped"
    )


# ---------------------------------------------------------------------------
# Deterministic client_order_id generation
# ---------------------------------------------------------------------------


def test_client_order_id_deterministic_across_instances() -> None:
    """Two bots with the same bot_id must generate the same client_order_id
    sequence regardless of when they were constructed (crash-recovery invariant).
    """
    bot_a = make_null_bot(bot_id="stable-id")
    bot_b = make_null_bot(bot_id="stable-id")

    ids_a = [bot_a._next_client_order_id() for _ in range(5)]
    ids_b = [bot_b._next_client_order_id() for _ in range(5)]

    assert ids_a == ids_b, "Same bot_id must produce identical client_order_id sequence"


def test_client_order_id_unique_per_sequence() -> None:
    """Each call to _next_client_order_id must return a distinct id."""
    bot = make_null_bot()
    ids = [bot._next_client_order_id() for _ in range(100)]
    assert len(set(ids)) == 100, "client_order_ids must be unique within a sequence"


def test_client_order_id_stable_after_rehydrate() -> None:
    """After rehydrate(), the id sequence continues from where it left off."""
    bot_a = make_null_bot(bot_id="stable-id")
    # Generate 3 ids, then snapshot
    for _ in range(3):
        bot_a._next_client_order_id()
    snap = bot_a.snapshot()

    # Fresh bot, rehydrate from snap
    bot_b = make_null_bot(bot_id="stable-id")
    bot_b.rehydrate(snap)

    # Next id from bot_b must equal what bot_a would produce next
    expected = bot_a._next_client_order_id()
    actual = bot_b._next_client_order_id()
    assert expected == actual, "Rehydrated bot must continue the same id sequence"
