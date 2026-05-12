"""SignalReplay — deterministic historical replay for backtests.

``SignalReplay`` implements :class:`~src.signals.base.SignalsProtocol` so that
backtests inject it in place of :class:`~src.signals.runner.SignalsRuntime`
without any changes to ``BaseBot`` or the strategy under test.

The backtest advances a virtual clock externally.  On each call to
``snapshot_for``, the replay returns the latest sample from ``signal_samples``
whose ``observed_at`` is at or before the virtual clock's current time.
"""

from __future__ import annotations

__all__ = ["SignalReplay"]

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from src.signals.base import SignalSnapshot
from src.signals.staleness import is_stale, merge_staleness
from src.signals.storage import SignalSample, SignalStorage

if TYPE_CHECKING:
    from src.bots.base import BotConfig, Clock
    from src.signals.registry import SignalRegistry

logger = logging.getLogger(__name__)


class SignalReplay:
    """Streams historical ``signal_samples`` rows through a virtual clock.

    ``SignalReplay`` satisfies :class:`~src.signals.base.SignalsProtocol`
    structurally — backtests do not need to import the runner at all.

    Args:
        registry: Signal registry used to look up ``tolerance_seconds`` per
            signal name.
        storage: :class:`~src.signals.storage.SignalStorage` backed by the
            ``signal_samples`` table (real Postgres or an in-memory stub).
        clock: Virtual clock.  Backtests advance this between ticks.
        start: Inclusive start of the replay window (``observed_at >= start``).
        end: Inclusive end of the replay window (``observed_at <= end``).
        params_hash_map: Mapping of signal_name → params_hash.  Used to query
            the correct rows from ``signal_samples``.  If a name is absent the
            replay logs a warning and marks the signal stale.
    """

    def __init__(
        self,
        registry: SignalRegistry,
        storage: SignalStorage,
        clock: Clock,
        start: datetime,
        end: datetime,
        params_hash_map: dict[str, str],
    ) -> None:
        self._registry = registry
        self._storage = storage
        self._clock = clock
        self._start = start
        self._end = end
        self._params_hash_map = params_hash_map

        # Pre-loaded samples per (signal_name, params_hash) sorted by observed_at.
        # Populated lazily on first snapshot_for call.
        self._loaded: dict[tuple[str, str], list[SignalSample]] = {}
        self._loaded_done = False

    async def _ensure_loaded(self) -> None:
        """Pre-load all relevant samples into memory once.

        Loads the full window up front to avoid repeated DB round-trips
        during replay.  For 30 days of 15-min samples (~2880 rows per signal)
        this is well within memory budget.
        """
        if self._loaded_done:
            return
        for name, params_hash in self._params_hash_map.items():
            key = (name, params_hash)
            rows: list[SignalSample] = []
            async for row in self._storage.replay(
                signal=name,
                params_hash=params_hash,
                start=self._start,
                end=self._end,
            ):
                rows.append(row)
            self._loaded[key] = rows
        self._loaded_done = True

    async def snapshot_for(self, config: BotConfig) -> SignalSnapshot:
        """Return the latest sample at or before the virtual clock's current time.

        Args:
            config: :class:`~src.bots.base.BotConfig` for the requesting bot.
                Uses ``config.signal_subscriptions`` to determine which signals
                to include.

        Returns:
            :class:`SignalSnapshot` with samples from ``signal_samples`` and
            staleness computed relative to the virtual clock.
        """
        await self._ensure_loaded()

        now = self._clock.now()
        samples: dict[str, Any] = {}
        stale_flags: dict[str, bool] = {}

        for sub in config.signal_subscriptions:
            name = sub if isinstance(sub, str) else sub.name
            params_hash = self._params_hash_map.get(name)
            if params_hash is None:
                logger.warning("SignalReplay: no params_hash for signal '%s'", name)
                stale_flags[name] = True
                continue

            key = (name, params_hash)
            rows = self._loaded.get(key, [])

            # Find the latest sample at or before virtual now.
            best: SignalSample | None = None
            for row in rows:
                if row.observed_at <= now:
                    best = row
                else:
                    break  # rows are sorted ascending; no point continuing

            if best is None:
                stale_flags[name] = True
                continue

            samples[name] = best.payload

            # Compute staleness from the registered tolerance.
            try:
                signal_cls = self._registry.get(name)
                tolerance = signal_cls.tolerance_seconds
            except KeyError:
                tolerance = 0  # unknown signal → always stale

            stale_flags[name] = is_stale(best.observed_at, tolerance, now)

        return SignalSnapshot(
            samples=samples,
            received_at=now,
            stale=merge_staleness(stale_flags),
        )
