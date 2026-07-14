"""``price_compare`` — the reference provisional winning rule for BTC Up/Down 5-min.

Compares the live BTC price (from the ``btc_15min`` signal — see
``src/signals/btc_15min.py``) against the price captured at position entry
(``ctx.position.entry_reference``), combined with which side the strategy
bought (``ctx.position.side`` — ``"UP"`` or ``"DOWN"``), to provisionally
decide WON / LOST / UNDECIDED.

Registered name: ``"polymarket.btc_up_or_down_5m.price_compare"``.

Flip-flopping caveat
---------------------
Unlike final settlement, this rule is recomputed every tick from live data
and can legitimately flip between WON/LOST/UNDECIDED multiple times before
the window actually closes (e.g. the BTC price oscillating right around the
entry reference). This is expected, not a bug. The optional
``params["tolerance_pct"]`` widens the "too close to call" band around the
entry reference to reduce (but not eliminate) flip-flopping right at the
boundary — a strategy consuming ``provisional_ruling()`` should still be
written defensively (e.g. only act after N consecutive ticks of the same
ruling).
"""

from __future__ import annotations

__all__ = ["PriceCompare"]

from decimal import Decimal, InvalidOperation
from typing import ClassVar

from src.rules.base import ProvisionalRuling, WinningRule, WinningRuleContext
from src.rules.registry import winning_rule

_SIGNAL_NAME = "btc_15min"


@winning_rule
class PriceCompare(WinningRule):
    """Compare live BTC price to the entry reference for the held side.

    Params:
        tolerance_pct: Optional ``Decimal``-coercible percentage (e.g. ``"0.05"``
            for 0.05%). The price must have moved by more than this percentage
            from ``entry_reference`` before the rule commits to WON/LOST.
            Defaults to ``Decimal("0")`` (any nonzero move commits).
    """

    name: ClassVar[str] = "polymarket.btc_up_or_down_5m.price_compare"

    def evaluate(self, ctx: WinningRuleContext) -> ProvisionalRuling:
        """Return WON/LOST/UNDECIDED by comparing live price to entry reference.

        Args:
            ctx: Position, signals, clock, and params for this evaluation.

        Returns:
            ``ProvisionalRuling.UNDECIDED`` if the ``btc_15min`` signal is
            missing or stale, if ``entry_reference`` is not positive, or if
            the price move is within ``tolerance_pct`` of the entry
            reference. Otherwise ``WON`` if the price moved in the held
            side's favor, ``LOST`` if against it.
        """
        if _SIGNAL_NAME in ctx.signals.stale or _SIGNAL_NAME not in ctx.signals.samples:
            return ProvisionalRuling.UNDECIDED

        sample = ctx.signals.samples[_SIGNAL_NAME]
        try:
            price = Decimal(str(sample["price_usd"]))
        except (KeyError, TypeError, InvalidOperation):
            return ProvisionalRuling.UNDECIDED

        entry = ctx.position.entry_reference
        if entry <= 0:
            return ProvisionalRuling.UNDECIDED

        try:
            tolerance_pct = Decimal(str(ctx.params.get("tolerance_pct", "0")))
        except InvalidOperation:
            tolerance_pct = Decimal("0")

        move_pct = abs(price - entry) / entry * Decimal("100")
        if move_pct <= tolerance_pct:
            return ProvisionalRuling.UNDECIDED

        price_went_up = price > entry
        side = ctx.position.side.upper()

        if side == "UP":
            return ProvisionalRuling.WON if price_went_up else ProvisionalRuling.LOST
        if side == "DOWN":
            return ProvisionalRuling.LOST if price_went_up else ProvisionalRuling.WON
        return ProvisionalRuling.UNDECIDED
