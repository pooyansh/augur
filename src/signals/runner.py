"""SignalsRuntime — shared cache + scheduler for all signal feeds.

Key properties:
- One background fetch loop per unique ``(signal_name, params_hash)`` tuple.
  50 bots subscribing to the same feed produce 1 upstream call per cadence.
- Multi-source fallback: sources are tried in order; first success wins.
- Staleness is data: if all sources fail and the cache exceeds
  ``tolerance_seconds``, the signal name is added to ``SignalSnapshot.stale``.
- Samples are persisted to ``signal_samples`` for backtest replay.

See .claude/rules/08-signals.md for the full design contract.
"""

from __future__ import annotations

__all__ = ["CachedSample", "SignalsRuntime", "SubscriptionHandle"]

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from src.signals.base import Signal, SignalSnapshot, SignalSource
from src.signals.registry import SignalRegistry
from src.signals.staleness import is_stale, merge_staleness
from src.signals.storage import SignalStorage

if TYPE_CHECKING:
    from src.bots.base import BotConfig, Clock

logger = logging.getLogger(__name__)

# Key type: (signal_name, params_hash)
_SubKey = tuple[str, str]


@dataclass
class CachedSample:
    """In-memory cache entry for one ``(signal_name, params_hash)`` subscription.

    Attributes:
        signal_name: The signal's ``name`` class attribute.
        params_hash: blake2s(8) hex of the sorted params dict.
        observed_at: UTC datetime of the last successful fetch.
        source: ``SignalSource.name`` that produced the cached value.
        payload: Canonical parsed value (output of ``Signal.parse``).
        fetch_failed_streak: Number of consecutive fetch cycles where all
            sources failed.  Reset to 0 on any success.
        last_fetch_attempt_failed: ``True`` if the most recent fetch cycle
            ended with all sources failing.
    """

    signal_name: str
    params_hash: str
    observed_at: datetime
    source: str
    payload: Any
    fetch_failed_streak: int = 0
    last_fetch_attempt_failed: bool = False


@dataclass
class SubscriptionHandle:
    """Opaque reference to an active subscription.

    Returned by :meth:`SignalsRuntime.subscribe`.  Callers do not need to
    interact with this directly; the runtime uses it internally.

    Attributes:
        signal_name: The signal's ``name``.
        params_hash: The params hash for this subscription.
    """

    signal_name: str
    params_hash: str


@dataclass
class _SubscriptionState:
    """Internal runtime state for one unique ``(signal_name, params_hash)``.

    Attributes:
        signal: Instantiated ``Signal`` object.
        sources: Instantiated ``SignalSource`` objects (one per source class).
        task: Background asyncio task running the fetch loop.
        cache: Latest cached sample (``None`` until first successful fetch).
        ref_count: Number of active :meth:`subscribe` calls sharing this loop.
        last_fetch_attempt_failed: ``True`` if the most recent fetch cycle
            ended with all sources failing.  Stored separately from the cache
            so a fresh failure can be detected even before the tolerance window
            expires.
    """

    signal: Signal
    sources: list[SignalSource]
    task: asyncio.Task[None] | None = field(default=None)
    cache: CachedSample | None = field(default=None)
    ref_count: int = field(default=0)
    last_fetch_attempt_failed: bool = field(default=False)


class SignalsRuntime:
    """Shared cache + scheduler that fans one upstream call out to N bots.

    Args:
        registry: :class:`~src.signals.registry.SignalRegistry` singleton used
            to look up signal classes by name.
        storage: :class:`~src.signals.storage.SignalStorage` for persisting
            samples to ``signal_samples``.
        clock: Injectable clock for deterministic tests.
        http: Shared ``httpx.AsyncClient`` passed to ``SignalSource.fetch``.
            The runtime does NOT own the client's lifecycle; callers must
            close it after ``stop()``.
    """

    def __init__(
        self,
        registry: SignalRegistry,
        storage: SignalStorage,
        clock: Clock,
        http: httpx.AsyncClient,
    ) -> None:
        self._registry = registry
        self._storage = storage
        self._clock = clock
        self._http = http

        # One _SubscriptionState per unique (signal_name, params_hash).
        self._subs: dict[_SubKey, _SubscriptionState] = {}
        self._started = False

    def subscribe(self, name: str, params: dict[str, Any]) -> SubscriptionHandle:
        """Declare a subscription to a signal feed.

        Idempotent on ``(name, params_hash)``: if a loop for this key is already
        running, this call simply increments the ref count and returns a handle.

        Args:
            name: Registered signal name (e.g. ``"btc_15min"``).
            params: Feed-specific parameters.

        Returns:
            :class:`SubscriptionHandle` referencing the active subscription.

        Raises:
            KeyError: If ``name`` is not in the registry.
        """
        signal_cls = self._registry.get(name)
        sig = signal_cls(params)
        key: _SubKey = (name, sig.params_hash)

        if key not in self._subs:
            sources = [src_cls() for src_cls in signal_cls.sources]
            state = _SubscriptionState(signal=sig, sources=sources)
            self._subs[key] = state

        self._subs[key].ref_count += 1

        # If the runtime is already started, launch the task immediately for
        # new subscriptions added after start().
        if self._started and self._subs[key].task is None:
            self._subs[key].task = asyncio.create_task(
                self._fetch_loop(key),
                name=f"signal-{name}-{sig.params_hash}",
            )

        return SubscriptionHandle(signal_name=name, params_hash=sig.params_hash)

    async def start(self) -> None:
        """Start a background fetch loop for every registered subscription.

        Safe to call only once.  Calling ``subscribe`` after ``start`` launches
        the loop immediately for the new subscription.
        """
        self._started = True
        for key, state in self._subs.items():
            if state.task is None:
                state.task = asyncio.create_task(
                    self._fetch_loop(key),
                    name=f"signal-{key[0]}-{key[1]}",
                )
        logger.info("SignalsRuntime started with %d subscription(s).", len(self._subs))

    async def stop(self) -> None:
        """Cancel all background fetch tasks.

        The caller is responsible for closing the shared ``httpx.AsyncClient``
        after ``stop()`` returns.
        """
        import contextlib

        for _key, state in self._subs.items():
            if state.task is not None and not state.task.done():
                state.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await state.task
                state.task = None
        logger.info("SignalsRuntime stopped.")

    async def snapshot_for(self, config: BotConfig) -> SignalSnapshot:
        """Assemble a :class:`SignalSnapshot` for the given bot.

        Looks up each signal subscription declared in ``config``, retrieves
        the cached sample, and computes staleness.

        Args:
            config: :class:`~src.bots.base.BotConfig` for the requesting bot.

        Returns:
            :class:`SignalSnapshot` with samples and staleness flags populated
            for all of the bot's subscriptions.  Signals not yet fetched
            (cache is ``None``) are included in ``stale``.
        """
        samples: dict[str, Any] = {}
        stale_flags: dict[str, bool] = {}
        now = self._clock.now()

        for sub in config.signal_subscriptions:
            name = sub if isinstance(sub, str) else sub.name
            # Find matching subscription state (params_hash unknown at this point;
            # scan by signal name).
            state = self._find_state_by_name(name)
            if state is None:
                logger.warning("snapshot_for: no subscription found for signal '%s'", name)
                stale_flags[name] = True
                continue

            cache = state.cache
            if cache is None:
                # No successful fetch yet.
                stale_flags[name] = True
                continue

            samples[name] = cache.payload
            tol = type(state.signal).tolerance_seconds
            # Stale = all sources have been failing AND the cached sample is
            # older than the tolerance window.
            stale_flags[name] = state.last_fetch_attempt_failed and is_stale(
                cache.observed_at, tol, now
            )

        return SignalSnapshot(
            samples=samples,
            received_at=now,
            stale=merge_staleness(stale_flags),
        )

    # ------------------------------------------------------------------
    # Internal fetch loop
    # ------------------------------------------------------------------

    async def _fetch_loop(self, key: _SubKey) -> None:
        """Background task: fetch → store → sleep → repeat.

        Args:
            key: ``(signal_name, params_hash)`` identifying the subscription.
        """
        state = self._subs[key]
        signal_name, params_hash = key
        cadence = type(state.signal).cadence_seconds

        # First fetch immediately; subsequent fetches after cadence sleep.
        while True:
            try:
                await self._do_fetch(key)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(
                    "Unexpected error in fetch loop for %s/%s: %s",
                    signal_name,
                    params_hash,
                    exc,
                )

            try:
                await asyncio.sleep(cadence)
            except asyncio.CancelledError:
                return

    async def _do_fetch(self, key: _SubKey) -> None:
        """Try each source in order; first success wins.  Updates the cache.

        Args:
            key: ``(signal_name, params_hash)`` identifying the subscription.
        """
        state = self._subs[key]
        signal_name, params_hash = key
        sig = state.signal
        params = dict(sig.params)

        for source in state.sources:
            t_start = time.monotonic()
            try:
                raw = await source.fetch(params)
            except Exception as exc:
                logger.warning(
                    "Signal '%s' source '%s' failed: %s",
                    signal_name,
                    source.name,
                    exc,
                )
                continue  # try next source

            latency_ms = int((time.monotonic() - t_start) * 1000)

            try:
                payload = sig.parse(source.name, raw)
            except Exception as exc:
                logger.warning(
                    "Signal '%s' source '%s' parse error: %s",
                    signal_name,
                    source.name,
                    exc,
                )
                continue  # treat parse failure as source failure

            observed_at = self._clock.now()

            # Persist to DB (best-effort; failure does not abort the fetch loop).
            try:
                payload_dict: dict[str, Any] = (
                    payload if isinstance(payload, dict) else {"value": payload}
                )
                await self._storage.append(
                    signal=signal_name,
                    params_hash=params_hash,
                    source=source.name,
                    observed_at=observed_at,
                    payload=payload_dict,
                    latency_ms=latency_ms,
                )
            except Exception as exc:
                logger.warning(
                    "Signal '%s' storage append failed (non-fatal): %s",
                    signal_name,
                    exc,
                )

            # Update in-memory cache.
            state.cache = CachedSample(
                signal_name=signal_name,
                params_hash=params_hash,
                observed_at=observed_at,
                source=source.name,
                payload=payload,
                fetch_failed_streak=0,
                last_fetch_attempt_failed=False,
            )
            state.last_fetch_attempt_failed = False
            logger.debug(
                "Signal '%s' fetched from '%s' in %dms",
                signal_name,
                source.name,
                latency_ms,
            )
            return  # success — stop trying sources

        # All sources failed.
        streak = (state.cache.fetch_failed_streak + 1) if state.cache else 1
        if state.cache is not None:
            state.cache = CachedSample(
                signal_name=state.cache.signal_name,
                params_hash=state.cache.params_hash,
                observed_at=state.cache.observed_at,
                source=state.cache.source,
                payload=state.cache.payload,
                fetch_failed_streak=streak,
                last_fetch_attempt_failed=True,
            )
        state.last_fetch_attempt_failed = True
        logger.warning(
            "Signal '%s' all sources failed (streak=%d); serving stale cache.",
            signal_name,
            streak,
        )

    def _find_state_by_name(self, signal_name: str) -> _SubscriptionState | None:
        """Return the first subscription state matching ``signal_name``, or ``None``.

        In practice each (name, params_hash) is unique; for bots that subscribe
        to a signal with default empty params there is exactly one state entry.
        This is an O(n) scan acceptable for the number of unique subscriptions.

        Args:
            signal_name: Signal name to look up.

        Returns:
            The :class:`_SubscriptionState` or ``None`` if not found.
        """
        for (name, _hash), state in self._subs.items():
            if name == signal_name:
                return state
        return None
