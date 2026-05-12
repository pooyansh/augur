"""Risk cap dataclass and enforcement helper.

:class:`RiskCaps` was previously defined in ``src.bots.base``; it now lives
here so the caps logic can be imported independently of the full bot stack.
``src.bots.base`` re-exports it for backward compatibility.

The ``check_caps`` function is the single enforcement point for all three
per-bot caps.  It is called by :meth:`~src.bots.base.BaseBot._check_risk_caps`
and is covered by a Hypothesis property suite in
``tests/unit/test_risk_caps.py``.
"""

from __future__ import annotations

__all__ = [
    "RiskCapExceeded",
    "RiskCaps",
    "check_caps",
]

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


class RiskCapExceeded(RuntimeError):  # noqa: N818 — name prescribed by CLAUDE.md Phase 3 spec
    """Raised when an order intent would violate a configured risk cap.

    Caught by :meth:`~src.bots.base.BaseBot.place` which records a
    ``RejectionEvent`` with ``category="risk"`` in the audit log and does NOT
    forward to the adapter.
    """


@dataclass
class RiskCaps:
    """Per-bot risk limits.

    All three caps are mandatory — every :class:`~src.bots.base.BotConfig`
    must carry explicit values.  There is no implicit global default.

    Args:
        max_position_notional: Maximum open position in collateral units.
            New orders whose notional added to the current position would
            exceed this are rejected.
        max_daily_loss: Maximum cumulative loss allowed today (positive
            Decimal).  When ``daily_loss >= max_daily_loss`` every new order
            is rejected until the reset boundary (midnight UTC).
        max_orders_per_minute: Burst protection — maximum orders placed in
            any rolling 60-second window.  Setting this to 0 effectively
            freezes trading.
    """

    max_position_notional: Decimal
    max_daily_loss: Decimal
    max_orders_per_minute: int


def check_caps(
    price: Decimal,
    size: Decimal,
    *,
    position_notional: Decimal,
    daily_loss: Decimal,
    recent_order_times: list[datetime],
    caps: RiskCaps,
    now: datetime | None = None,
) -> None:
    """Validate a prospective order against all three risk caps.

    Designed to be a pure function — no I/O, no side-effects.
    ``BaseBot._check_risk_caps`` calls this helper so the logic is testable
    independently of the full bot wiring.

    The ``recent_order_times`` list is **not** mutated by this function.
    Callers append the current timestamp to it *after* a successful check.

    Args:
        price: Order limit price.
        size: Order size in shares/contracts.
        position_notional: Current open position notional for this bot.
        daily_loss: Cumulative daily loss so far (positive = loss).
        recent_order_times: UTC timestamps of orders placed in the last 60 s
            (pruned by the caller).
        caps: The caps to enforce.
        now: Reference UTC datetime.  Defaults to ``datetime.now(UTC)``.

    Raises:
        RiskCapExceeded: If any cap would be breached.
    """
    if now is None:
        now = datetime.now(tz=UTC)

    notional = price * size

    # Cap 1 — position notional
    if position_notional + notional > caps.max_position_notional:
        raise RiskCapExceeded(
            f"Position notional cap {caps.max_position_notional} would be exceeded "
            f"(current={position_notional}, adding={notional})"
        )

    # Cap 2 — daily loss
    if daily_loss >= caps.max_daily_loss:
        raise RiskCapExceeded(
            f"Daily loss cap {caps.max_daily_loss} reached (current={daily_loss})"
        )

    # Cap 3 — order rate (rolling 60-second window)
    window_start = now.timestamp() - 60.0
    orders_in_window = sum(1 for t in recent_order_times if t.timestamp() >= window_start)
    if orders_in_window >= caps.max_orders_per_minute:
        raise RiskCapExceeded(
            f"Order rate cap {caps.max_orders_per_minute}/min exceeded (recent={orders_in_window})"
        )
