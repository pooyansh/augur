"""Signal ABC and Phase 3 in-memory stub.

Phase 3a builds the shared cache + scheduler that ensures N bots subscribed
to the same feed produce 1 upstream call.  This module provides only the
abstract contract and a no-op stub so BaseBot can compile and tests can run.
"""

from __future__ import annotations

__all__ = [
    "InMemorySignals",
    "Signal",
]

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


class Signal(ABC):
    """Abstract base for a time-series signal feed.

    Each concrete subclass fetches one external data source (e.g. BTC/USD
    price from Binance, polling averages from a data vendor).  Subclasses
    declare their refresh cadence; the signals runner (Phase 3a) schedules
    them so multiple bots sharing a feed share one upstream call.

    Class attributes:
        name: Unique registry key (e.g. ``"btc_usd_price"``).
        cadence_seconds: How often to refresh this signal.
        tolerance_seconds: Staleness boundary — if the last fetch is older
            than this, the signal is marked stale in :class:`SignalSnapshot`
            and a ``warn`` is emitted.
    """

    name: ClassVar[str]
    cadence_seconds: ClassVar[int]
    tolerance_seconds: ClassVar[int]

    @abstractmethod
    async def fetch(self) -> Any:
        """Fetch the latest value for this signal.

        Returns:
            The signal value.  Return shape is declared by the concrete
            subclass and must be documented there.

        Raises:
            Exception: Any network or parsing error.  The runner catches and
                marks the signal stale rather than crashing the bot.
        """


# ---------------------------------------------------------------------------
# Phase 3 stub — replaced by the real runner in Phase 3a
# ---------------------------------------------------------------------------


class InMemorySignals:
    """Phase 3 stub — returns an empty :class:`SignalSnapshot` every call.

    The real runner (Phase 3a) will share feeds across bots, check staleness,
    and emit warn events when a feed goes stale.  This stub satisfies the
    interface so BaseBot and tests compile without a full runner.
    """

    async def snapshot_for(self, config: Any) -> SignalSnapshot:
        """Return an empty snapshot for the given bot config.

        Args:
            config: :class:`~src.bots.base.BotConfig` for the requesting bot.
                Ignored in the stub; real runner uses it to select feeds.

        Returns:
            A :class:`SignalSnapshot` with no samples and no stale signals.
        """
        # Import here to avoid a circular import at module load time.
        from src.bots.base import SignalSnapshot

        return SignalSnapshot(
            samples={},
            received_at=datetime.now(tz=UTC),
            stale=frozenset(),
        )


# ---------------------------------------------------------------------------
# SignalSnapshot (defined here to keep signals self-contained)
# ---------------------------------------------------------------------------


class SignalSnapshot:
    """Immutable snapshot of all signal values delivered to a bot each tick.

    Attributes:
        samples: Mapping of signal name → latest value.
        received_at: UTC datetime when this snapshot was assembled.
        stale: Subset of subscribed signal names whose last fetch exceeded
            their ``tolerance_seconds``.  Strategies should treat stale
            signals with caution.
    """

    __slots__ = ("received_at", "samples", "stale")

    def __init__(
        self,
        samples: Mapping[str, Any],
        received_at: datetime,
        stale: frozenset[str],
    ) -> None:
        self.samples = samples
        self.received_at = received_at
        self.stale = stale
