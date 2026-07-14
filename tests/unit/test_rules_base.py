"""Tests for WinningRuleContext/PositionState construction and the concrete
``price_compare`` rule's ``evaluate()`` behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from src.rules.base import PositionState, ProvisionalRuling, WinningRuleContext
from src.rules.polymarket.btc_up_or_down_5m.price_compare import PriceCompare
from src.signals.base import SignalSnapshot

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _snapshot(
    price_usd: str | None,
    *,
    stale: bool = False,
) -> SignalSnapshot:
    samples: dict[str, object] = {}
    if price_usd is not None:
        samples["btc_15min"] = {"price_usd": price_usd, "source": "coingecko", "source_ts": "x"}
    return SignalSnapshot(
        samples=samples,
        received_at=_NOW,
        stale=frozenset({"btc_15min"}) if stale else frozenset(),
    )


def _position(side: str, entry_reference: str) -> PositionState:
    return PositionState(
        market_id="market-1",
        side=side,
        entry_reference=Decimal(entry_reference),
        entry_at=_NOW,
        size=Decimal("10"),
    )


def test_winning_rule_context_and_position_state_construction() -> None:
    """PositionState and WinningRuleContext construct with the documented fields."""
    position = _position("UP", "100000")
    ctx = WinningRuleContext(
        position=position,
        signals=_snapshot("100050"),
        now=_NOW,
        params={},
    )
    assert ctx.position.market_id == "market-1"
    assert ctx.position.side == "UP"
    assert ctx.position.entry_reference == Decimal("100000")
    assert ctx.now == _NOW
    assert ctx.params == {}


@pytest.mark.parametrize(
    "side,price,expected",
    [
        ("UP", "100050", ProvisionalRuling.WON),
        ("UP", "99950", ProvisionalRuling.LOST),
        ("DOWN", "99950", ProvisionalRuling.WON),
        ("DOWN", "100050", ProvisionalRuling.LOST),
        ("up", "100050", ProvisionalRuling.WON),  # case-insensitive side
    ],
)
def test_price_compare_won_and_lost(side: str, price: str, expected: ProvisionalRuling) -> None:
    """Price moving favorably/unfavorably yields WON/LOST for the held side."""
    rule = PriceCompare()
    ctx = WinningRuleContext(
        position=_position(side, "100000"),
        signals=_snapshot(price),
        now=_NOW,
        params={},
    )
    assert rule.evaluate(ctx) == expected


def test_price_compare_equal_price_is_undecided() -> None:
    """Price exactly at entry_reference is UNDECIDED — no move at all."""
    rule = PriceCompare()
    ctx = WinningRuleContext(
        position=_position("UP", "100000"),
        signals=_snapshot("100000"),
        now=_NOW,
        params={},
    )
    assert rule.evaluate(ctx) == ProvisionalRuling.UNDECIDED


def test_price_compare_missing_signal_is_undecided() -> None:
    """Missing btc_15min sample yields UNDECIDED, never raises."""
    rule = PriceCompare()
    ctx = WinningRuleContext(
        position=_position("UP", "100000"),
        signals=_snapshot(None),
        now=_NOW,
        params={},
    )
    assert rule.evaluate(ctx) == ProvisionalRuling.UNDECIDED


def test_price_compare_stale_signal_is_undecided() -> None:
    """Stale btc_15min signal yields UNDECIDED even if a sample is present."""
    rule = PriceCompare()
    ctx = WinningRuleContext(
        position=_position("UP", "100000"),
        signals=_snapshot("105000", stale=True),
        now=_NOW,
        params={},
    )
    assert rule.evaluate(ctx) == ProvisionalRuling.UNDECIDED


def test_price_compare_within_tolerance_is_undecided() -> None:
    """A move smaller than tolerance_pct stays UNDECIDED."""
    rule = PriceCompare()
    ctx = WinningRuleContext(
        position=_position("UP", "100000"),
        signals=_snapshot("100010"),  # 0.01% move
        now=_NOW,
        params={"tolerance_pct": "0.05"},
    )
    assert rule.evaluate(ctx) == ProvisionalRuling.UNDECIDED


def test_price_compare_beyond_tolerance_commits() -> None:
    """A move larger than tolerance_pct commits to WON/LOST."""
    rule = PriceCompare()
    ctx = WinningRuleContext(
        position=_position("UP", "100000"),
        signals=_snapshot("100100"),  # 0.1% move, favorable
        now=_NOW,
        params={"tolerance_pct": "0.05"},
    )
    assert rule.evaluate(ctx) == ProvisionalRuling.WON


def test_price_compare_unknown_side_is_undecided() -> None:
    """An unrecognized side label is a safe UNDECIDED, not a crash."""
    rule = PriceCompare()
    ctx = WinningRuleContext(
        position=_position("SIDEWAYS", "100000"),
        signals=_snapshot("100050"),
        now=_NOW,
        params={},
    )
    assert rule.evaluate(ctx) == ProvisionalRuling.UNDECIDED


def test_price_compare_nonpositive_entry_reference_is_undecided() -> None:
    """A zero/negative entry_reference (shouldn't happen, but must not crash) is UNDECIDED."""
    rule = PriceCompare()
    ctx = WinningRuleContext(
        position=_position("UP", "0"),
        signals=_snapshot("100050"),
        now=_NOW,
        params={},
    )
    assert rule.evaluate(ctx) == ProvisionalRuling.UNDECIDED
