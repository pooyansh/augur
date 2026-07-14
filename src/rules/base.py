"""Provisional winning rule ABC and shared data types.

A ``WinningRule`` is a pure, synchronous, deterministic judgment call about
whether a strategy's currently-held position is likely to win or lose a
market window — computed independently of (and much faster than) real
exchange settlement.  It never performs I/O and never mutates anything; it
only reads the ``SignalSnapshot`` already fetched this tick plus the
strategy's own reported ``PositionState``.

See ``.claude/rules/10-winning-rules.md`` for the full design contract,
including the hard invariant that a provisional ruling must never feed P&L
bookkeeping, ``snapshot()``'s ``position`` field, or any audit kind that
implies real settlement.
"""

from __future__ import annotations

__all__ = [
    "PositionState",
    "ProvisionalRuling",
    "WinningRule",
    "WinningRuleContext",
]

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from src.signals.base import SignalSnapshot


class ProvisionalRuling(StrEnum):
    """A rule's early, non-authoritative judgment of a held position's outcome.

    ``UNDECIDED`` is the safe default — used whenever there isn't enough
    information yet to call a winner, and strategies should treat it exactly
    like "no ruling available."
    """

    WON = "won"
    LOST = "lost"
    UNDECIDED = "undecided"


@dataclass(frozen=True, slots=True)
class PositionState:
    """What a strategy currently holds for a market — supplied by the strategy itself.

    ``BaseBot`` has no generic notion of "current position"; strategies that
    want the provisional-ruling feature implement ``current_position()`` to
    expose this, typically alongside whatever they already track for their
    own ``snapshot()``/``rehydrate()``.

    Args:
        market_id: Canonical market identifier the position is held in.
        side: Market-family-specific side label the strategy bought
            (e.g. ``"UP"``/``"DOWN"`` for a BTC up-or-down window).
        entry_reference: Reference value captured at entry (e.g. the price
            the position needs to beat). Always ``Decimal`` — never ``float``.
        entry_at: UTC datetime the position was opened.
        size: Position size, in whatever units the strategy tracks (shares,
            notional, etc.) — always ``Decimal``.
    """

    market_id: str
    side: str
    entry_reference: Decimal
    entry_at: datetime
    size: Decimal


@dataclass(frozen=True, slots=True)
class WinningRuleContext:
    """Everything a ``WinningRule.evaluate`` call needs — no I/O, no side effects.

    Args:
        position: The strategy's currently-held position.
        signals: The same ``SignalSnapshot`` already passed to ``on_tick``
            this tick.  A winning rule never triggers its own fetch.
        now: Current UTC datetime (from the bot's injectable clock).
        params: Rule-specific parameters from the bot's config
            (``BotEntry.winning_rule.params`` in ``config/bots.yaml``).
    """

    position: PositionState
    signals: SignalSnapshot
    now: datetime
    params: dict[str, Any]


class WinningRule(ABC):
    """Abstract base for a pluggable, market-scoped provisional winning rule.

    Concrete subclasses live under ``src/rules/<venue>/<series_slug>/`` and
    register themselves with a fully-qualified dotted name of the form
    ``"<venue>.<series_slug>.<rule_name>"`` via the ``@winning_rule`` decorator
    (``src/rules/registry.py``).

    ``evaluate`` MUST be pure and deterministic: same inputs always produce
    the same output, no network calls, no mutation of ``ctx`` or ``self``
    beyond what's needed for the single call.  This makes every rule fully
    unit-testable without mocks.

    Class attributes:
        name: Fully-qualified dotted registry key, e.g.
            ``"polymarket.btc_up_or_down_5m.price_compare"``.
    """

    name: ClassVar[str]

    @abstractmethod
    def evaluate(self, ctx: WinningRuleContext) -> ProvisionalRuling:
        """Compute a provisional ruling for the held position.

        Args:
            ctx: Position, signals, clock, and params for this evaluation.

        Returns:
            ``ProvisionalRuling.WON``, ``LOST``, or ``UNDECIDED``.  Return
            ``UNDECIDED`` whenever there isn't enough information to safely
            commit to a direction — never raise for "don't know yet."
        """
