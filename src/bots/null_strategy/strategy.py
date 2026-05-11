"""NullStrategy — production-registered no-op strategy for testing and scaffolding.

This module re-exports the :class:`~tests.fixtures.null_strategy.NullStrategy`
from the test fixtures so that auto-discovery finds it via
``src/bots/null_strategy/strategy.py`` and registers it with the global
:data:`~src.manager.registry.registry`.

The strategy is safe to run in paper mode against the echo exchange.
"""

from __future__ import annotations

__all__ = ["NullStrategy"]

from decimal import Decimal
from typing import Any, ClassVar

from src.bots.base import BaseBot, BotConfig, BotDeps, Decision, Schedule
from src.exchanges.base import Market
from src.manager.registry import registry
from src.signals.base import SignalSnapshot


@registry.strategy
class NullStrategy(BaseBot):
    """No-op strategy that returns an empty :class:`~src.bots.base.Decision` each tick.

    Registered under the name ``"null_strategy"``.  Designed for paper-mode
    testing with the echo exchange.

    Attributes:
        tick_count: Number of completed ``on_tick`` calls.
    """

    name: ClassVar[str] = "null_strategy"
    schedule: ClassVar[Schedule] = Schedule(every_seconds=60)

    def __init__(self, market: Market, config: BotConfig, deps: BotDeps) -> None:
        super().__init__(market, config, deps)
        self.tick_count: int = 0

    async def on_tick(self, signals: SignalSnapshot) -> Decision:
        """Increment ``tick_count`` and return an empty decision.

        Args:
            signals: Current signal snapshot (ignored).

        Returns:
            Empty :class:`~src.bots.base.Decision`.
        """
        self.tick_count += 1
        return Decision()

    def snapshot(self) -> dict[str, Any]:
        """Serialise the minimal required state plus ``tick_count``.

        Returns:
            Dict with ``intent_seq``, ``position``, ``last_decision_at``,
            and ``tick_count``.
        """
        return {
            "intent_seq": self._intent_seq,
            "position": str(self._position_notional),
            "last_decision_at": self._deps.clock.now().isoformat(),
            "tick_count": self.tick_count,
        }

    def rehydrate(self, snapshot: dict[str, Any]) -> None:
        """Restore state from a snapshot.

        Args:
            snapshot: Dict produced by :meth:`snapshot`.
        """
        self._intent_seq = int(snapshot.get("intent_seq", 0))
        self._position_notional = Decimal(str(snapshot.get("position", "0")))
        self.tick_count = int(snapshot.get("tick_count", 0))
