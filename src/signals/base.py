"""Signal ABC, SignalSource ABC, SignalsProtocol, and shared data types.

Phase 3a: full signals platform.  This module provides the abstract contracts
that all signal implementations and runtime/replay consumers must satisfy.

Key design decisions (see .claude/rules/08-signals.md):
- ``Signal`` is stateless w.r.t. consumers.  State = ``signal_samples`` rows.
- Fetching is the ``SignalSource``'s job; the runner controls when and how often.
- Staleness is data (``SignalSnapshot.stale``), not an exception.
- Feature engineering lives in strategies, not in ``Signal`` subclasses.
"""

from __future__ import annotations

__all__ = [
    "InMemorySignals",
    "Signal",
    "SignalSnapshot",
    "SignalSource",
    "SignalsProtocol",
]

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import blake2s
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.bots.base import BotConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SignalSource ABC
# ---------------------------------------------------------------------------


class SignalSource(ABC):
    """Abstract base for a single upstream data provider.

    A ``Signal`` declares an ordered list of ``SignalSource`` subclasses
    (``Signal.sources``).  The runner tries them in order; first success wins.
    Failures are logged and counted but do NOT propagate to the consumer.

    Class attributes:
        name: Unique label for this source within its signal (e.g. ``"coingecko"``).
            Used in ``signal_samples.source`` and per-source failure metrics.
    """

    name: ClassVar[str]

    @abstractmethod
    async def fetch(self, params: Mapping[str, Any]) -> Any:
        """Fetch the latest raw value from this source.

        The runner times this call; its duration becomes ``latency_ms`` in
        ``signal_samples``.  Do not parse or validate here — parsing belongs
        in ``Signal.parse``.

        Args:
            params: Signal-level parameters (e.g. ``{"symbol": "BTC"}``).
                Same ``params`` object passed to the owning ``Signal.__init__``.

        Returns:
            Raw response object (dict, bytes, etc.).  Shape is source-specific.

        Raises:
            Exception: Any network, timeout, or rate-limit error.  The runner
                catches these, records a failure, and tries the next source.
        """


# ---------------------------------------------------------------------------
# Signal ABC
# ---------------------------------------------------------------------------


class Signal(ABC):
    """Abstract base for a time-series signal feed.

    Each concrete subclass represents one logical signal (e.g. BTC/USD price)
    and declares its sources, cadence, and freshness tolerance.  Multiple bots
    subscribing to the same ``(name, params)`` share exactly one upstream call
    per cadence interval — the runner enforces this.

    Class attributes:
        name: Unique registry key (e.g. ``"btc_15min"``).
        cadence_seconds: How often the runner fetches a fresh sample.
        tolerance_seconds: Staleness boundary.  If the last successful fetch is
            older than this, the signal is added to ``SignalSnapshot.stale``.
        sources: Ordered list of ``SignalSource`` *classes* (not instances).
            The runner tries them left-to-right; first success wins.  Adding a
            fallback is a code change to the signal class, not a config change.

    Args:
        params: Feed-specific parameters.  Two bots sharing the same ``(name,
            params)`` share a single fetch loop — params_hash deduplicates them.
    """

    name: ClassVar[str]
    cadence_seconds: ClassVar[int]
    tolerance_seconds: ClassVar[int]
    sources: ClassVar[list[type[SignalSource]]]

    def __init__(self, params: Mapping[str, Any]) -> None:
        self._params: Mapping[str, Any] = params

    @property
    def params(self) -> Mapping[str, Any]:
        """The params mapping this signal was instantiated with."""
        return self._params

    @property
    def params_hash(self) -> str:
        """Stable 8-byte hex hash of the sorted params dict.

        Two ``Signal`` instances with identical ``params`` have the same hash —
        this is the deduplication key used in the runner and ``signal_samples``.

        Returns:
            16-character lowercase hex string.
        """
        raw = json.dumps(self._params, sort_keys=True).encode()
        return blake2s(raw, digest_size=8).hexdigest()

    @abstractmethod
    def parse(self, source_name: str, raw: Any) -> Any:
        """Parse a raw source response into the canonical signal shape.

        Called by the runner immediately after a successful ``source.fetch``.
        Implementations must be pure (no I/O) and raise on malformed input.

        Args:
            source_name: ``SignalSource.name`` value of the source that fetched.
            raw: The raw response object returned by ``source.fetch``.

        Returns:
            Canonical parsed value.  Shape is specific to this signal subclass
            and must be documented on the subclass.

        Raises:
            ValueError: If ``raw`` cannot be parsed into the expected shape.
        """


# ---------------------------------------------------------------------------
# SignalSnapshot
# ---------------------------------------------------------------------------


class SignalSnapshot:
    """Immutable snapshot of all signal values delivered to a bot each tick.

    Attributes:
        samples: Mapping of signal name to latest canonical parsed value.
            Values are whatever ``Signal.parse`` returned for that signal.
        received_at: UTC datetime when this snapshot was assembled.
        stale: Frozenset of signal names whose last successful fetch exceeded
            their ``tolerance_seconds``.  Strategies should treat stale signals
            with caution (e.g. reduce position size, skip the tick).
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


# ---------------------------------------------------------------------------
# SignalsProtocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SignalsProtocol(Protocol):
    """Structural protocol satisfied by both ``SignalsRuntime`` and ``SignalReplay``.

    ``BotDeps.signals`` is typed as ``SignalsProtocol``.  Tests can inject any
    object that implements this single method — including ``InMemorySignals``.
    """

    async def snapshot_for(self, config: BotConfig) -> SignalSnapshot:
        """Return a signal snapshot for the requesting bot.

        Args:
            config: :class:`~src.bots.base.BotConfig` for the requesting bot.
                Implementations use ``config.signal_subscriptions`` to select
                which feeds to include.

        Returns:
            :class:`SignalSnapshot` with current samples and staleness info.
        """
        ...


# ---------------------------------------------------------------------------
# InMemorySignals — Phase 3 stub; kept for tests that don't need a runner
# ---------------------------------------------------------------------------


class InMemorySignals:
    """Phase 3 stub — returns an empty :class:`SignalSnapshot` every call.

    The real runner (``SignalsRuntime``) shares feeds across bots, checks
    staleness, and persists samples.  This stub satisfies the ``SignalsProtocol``
    so ``BaseBot`` and tests compile without a full runner.

    Satisfies :class:`SignalsProtocol` structurally.
    """

    async def snapshot_for(self, config: Any) -> SignalSnapshot:
        """Return an empty snapshot for the given bot config.

        Args:
            config: :class:`~src.bots.base.BotConfig` for the requesting bot.
                Ignored in the stub; real runner uses it to select feeds.

        Returns:
            A :class:`SignalSnapshot` with no samples and no stale signals.
        """
        return SignalSnapshot(
            samples={},
            received_at=datetime.now(tz=UTC),
            stale=frozenset(),
        )
