"""Hypothesis property tests for the risk-cap enforcement logic.

Tests operate on :func:`~src.risk.caps.check_caps` directly (pure function)
and also verify that the same exceptions are importable from the legacy path
``src.bots.base`` for backward compatibility.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from src.risk.caps import RiskCapExceeded, RiskCaps, check_caps

# ---------------------------------------------------------------------------
# Re-export backward-compat test
# ---------------------------------------------------------------------------


def test_risk_cap_exceeded_importable_from_bots_base() -> None:
    """RiskCapExceeded must still be importable from src.bots.base."""
    from src.bots.base import RiskCapExceeded as BasePath

    assert BasePath is RiskCapExceeded


def test_risk_caps_importable_from_bots_base() -> None:
    """RiskCaps must still be importable from src.bots.base."""
    from src.bots.base import RiskCaps as BasePath

    assert BasePath is RiskCaps


# ---------------------------------------------------------------------------
# Shared Hypothesis strategies
# ---------------------------------------------------------------------------

decimal_price = st.decimals(
    min_value="0.01",
    max_value="0.99",
    places=2,
    allow_nan=False,
    allow_infinity=False,
)

decimal_size = st.decimals(
    min_value="1",
    max_value="100",
    places=0,
    allow_nan=False,
    allow_infinity=False,
)


def _caps(
    max_pos: Decimal = Decimal("10000"),
    max_loss: Decimal = Decimal("1000"),
    max_rate: int = 60,
) -> RiskCaps:
    return RiskCaps(
        max_position_notional=max_pos,
        max_daily_loss=max_loss,
        max_orders_per_minute=max_rate,
    )


_NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Property: position-notional cap
# ---------------------------------------------------------------------------


@given(price=decimal_price, size=decimal_size)
@settings(max_examples=100)
def test_check_caps_accepts_within_position_limit(price: Decimal, size: Decimal) -> None:
    """check_caps must pass when position + notional stays within cap."""
    caps = _caps(max_pos=Decimal("99999"))
    check_caps(
        price,
        size,
        position_notional=Decimal(0),
        daily_loss=Decimal(0),
        recent_order_times=[],
        caps=caps,
        now=_NOW,
    )  # must not raise


@given(price=decimal_price, size=decimal_size)
@settings(max_examples=100)
def test_check_caps_blocks_position_cap_exceeded(price: Decimal, size: Decimal) -> None:
    """Notional that pushes position above cap must raise RiskCapExceeded."""
    caps = _caps(max_pos=Decimal("0"))  # cap at 0 — any order exceeds it
    with pytest.raises(RiskCapExceeded, match="Position notional cap"):
        check_caps(
            price,
            size,
            position_notional=Decimal(0),
            daily_loss=Decimal(0),
            recent_order_times=[],
            caps=caps,
            now=_NOW,
        )


# ---------------------------------------------------------------------------
# Property: daily-loss cap
# ---------------------------------------------------------------------------


@given(price=decimal_price, size=decimal_size)
@settings(max_examples=100)
def test_check_caps_blocks_when_daily_loss_at_cap(price: Decimal, size: Decimal) -> None:
    """When daily_loss >= max_daily_loss every order must be rejected."""
    caps = _caps(max_loss=Decimal("0"))
    with pytest.raises(RiskCapExceeded, match="Daily loss cap"):
        check_caps(
            price,
            size,
            position_notional=Decimal(0),
            daily_loss=Decimal(0),  # 0 >= 0 → reject
            recent_order_times=[],
            caps=caps,
            now=_NOW,
        )


# ---------------------------------------------------------------------------
# Property: order-rate cap
# ---------------------------------------------------------------------------


@given(price=decimal_price, size=decimal_size)
@settings(max_examples=100)
def test_check_caps_blocks_when_rate_exceeded(price: Decimal, size: Decimal) -> None:
    """With max_orders_per_minute=0, every order must be rejected."""
    caps = _caps(max_rate=0)
    with pytest.raises(RiskCapExceeded, match="Order rate cap"):
        check_caps(
            price,
            size,
            position_notional=Decimal(0),
            daily_loss=Decimal(0),
            recent_order_times=[],
            caps=caps,
            now=_NOW,
        )


def test_check_caps_rate_window_is_rolling_60s() -> None:
    """Order times older than 60 seconds must be excluded from the window."""
    caps = _caps(max_rate=1)
    old_time = datetime(2025, 1, 1, 11, 58, 0, tzinfo=UTC)  # 2 min ago
    # One stale entry — it should not count, so the check should pass.
    check_caps(
        Decimal("0.50"),
        Decimal("1"),
        position_notional=Decimal(0),
        daily_loss=Decimal(0),
        recent_order_times=[old_time],
        caps=caps,
        now=_NOW,
    )  # must not raise


def test_check_caps_rate_cap_counts_recent_times() -> None:
    """A recent order in the window must count toward the cap."""
    caps = _caps(max_rate=1)
    recent_time = datetime(2025, 1, 1, 11, 59, 30, tzinfo=UTC)  # 30 s ago
    with pytest.raises(RiskCapExceeded, match="Order rate cap"):
        check_caps(
            Decimal("0.50"),
            Decimal("1"),
            position_notional=Decimal(0),
            daily_loss=Decimal(0),
            recent_order_times=[recent_time],
            caps=caps,
            now=_NOW,
        )
