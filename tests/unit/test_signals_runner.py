"""Tests for SignalsRuntime — dedup, fallback, and staleness behaviour."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.bots.base import BotConfig, Clock, RiskCaps, Schedule
from src.exchanges.base import Mode
from src.signals.base import Signal, SignalSnapshot, SignalSource
from src.signals.registry import SignalRegistry
from src.signals.runner import SignalsRuntime
from src.signals.storage import SignalStorage

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class FixedClock(Clock):
    """Clock whose time can be set by tests."""

    def __init__(self, initial: datetime) -> None:
        self._now = initial

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _make_storage() -> SignalStorage:
    """Return a SignalStorage whose append is a no-op."""
    storage = MagicMock(spec=SignalStorage)
    storage.append = AsyncMock()
    return storage  # type: ignore[return-value]


def _make_bot_config(signal_name: str = "test_signal") -> BotConfig:
    return BotConfig(
        bot_id="test-bot",
        strategy_name="null",
        market_id="mkt-1",
        mode=Mode.PAPER,
        live=False,
        schedule=Schedule(every_seconds=60),
        risk=RiskCaps(
            max_position_notional=Decimal("1000"),
            max_daily_loss=Decimal("100"),
            max_orders_per_minute=10,
        ),
        signal_subscriptions=[signal_name],
    )


# ---------------------------------------------------------------------------
# Minimal test signal wired to a call-counting mock source
# ---------------------------------------------------------------------------


_call_count: int = 0


class CountingSource(SignalSource):
    name: ClassVar[str] = "counting"

    async def fetch(self, params: Mapping[str, Any]) -> Any:
        global _call_count
        _call_count += 1
        return {"price": "100.0"}


class AlwaysFailSource(SignalSource):
    name: ClassVar[str] = "always_fail"

    async def fetch(self, params: Mapping[str, Any]) -> Any:
        raise RuntimeError("Source intentionally down")


class BinanceLikeSource(SignalSource):
    name: ClassVar[str] = "binance_like"

    async def fetch(self, params: Mapping[str, Any]) -> Any:
        return {"price": "99.0"}


class CoingeckoLikeSource(SignalSource):
    """Returns HTTP 429-like error."""

    name: ClassVar[str] = "coingecko_like"

    async def fetch(self, params: Mapping[str, Any]) -> Any:
        raise RuntimeError("429 rate limited")


def _make_signal_cls(
    sig_name: str,
    cadence: int,
    tolerance: int,
    sources_list: list[type[SignalSource]],
) -> type[Signal]:
    """Dynamically create a Signal subclass for testing."""

    class TestSignal(Signal):
        name: ClassVar[str] = sig_name
        cadence_seconds: ClassVar[int] = cadence
        tolerance_seconds: ClassVar[int] = tolerance
        sources: ClassVar[list[type[SignalSource]]] = sources_list

        def parse(self, source_name: str, raw: Any) -> Any:
            return {"price": raw.get("price", "0"), "source": source_name}

    TestSignal.__name__ = sig_name
    return TestSignal


# ---------------------------------------------------------------------------
# Test: dedup invariant — 3 bots → 1 upstream call per cadence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_three_bots_one_fetch_per_cadence() -> None:
    """3 bots subscribed to the same signal produce 1 HTTP call per cadence."""
    global _call_count
    _call_count = 0

    # Use a very short cadence so the test completes in well under 1 second.
    cadence = 1  # 1 second cadence for testing
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    clock = FixedClock(t0)
    storage = _make_storage()

    sig_cls = _make_signal_cls("test_dedup", cadence, cadence * 2, [CountingSource])

    reg = SignalRegistry()
    reg.register(sig_cls)

    import httpx

    runtime = SignalsRuntime(
        registry=reg,
        storage=storage,
        clock=clock,
        http=httpx.AsyncClient(),
    )

    # Three bots subscribe to the same (signal, params={}).
    runtime.subscribe("test_dedup", {})
    runtime.subscribe("test_dedup", {})  # idempotent
    runtime.subscribe("test_dedup", {})  # idempotent

    # Only ONE subscription state should exist.
    assert len(runtime._subs) == 1, "Expected exactly one subscription key"

    await runtime.start()

    # Let the first fetch complete.
    await asyncio.sleep(0.05)
    assert _call_count == 1, f"Expected 1 fetch, got {_call_count}"

    # Wait for the second cadence (1 second cadence → 2nd fetch at t=1s).
    await asyncio.sleep(cadence + 0.2)
    assert _call_count == 2, f"Expected 2 fetches after 2 cadences, got {_call_count}"

    await runtime.stop()


# ---------------------------------------------------------------------------
# Test: fallback — primary fails (429), secondary succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_secondary_used_when_primary_fails() -> None:
    """When the first source returns an error, the runner tries the second."""
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    clock = FixedClock(t0)
    storage = _make_storage()

    sig_cls = _make_signal_cls(
        "test_fallback",
        cadence=100,
        tolerance=200,
        sources_list=[CoingeckoLikeSource, BinanceLikeSource],
    )

    reg = SignalRegistry()
    reg.register(sig_cls)

    import httpx

    runtime = SignalsRuntime(
        registry=reg,
        storage=storage,
        clock=clock,
        http=httpx.AsyncClient(),
    )
    runtime.subscribe("test_fallback", {})
    await runtime.start()

    # Allow the fetch to complete.
    await asyncio.sleep(0.05)

    # The cache should be populated from the Binance-like (fallback) source.
    key = ("test_fallback", next(iter(runtime._subs.values())).signal.params_hash)
    state = runtime._subs[key]
    assert state.cache is not None, "Cache should be populated after successful fallback"
    assert state.cache.source == "binance_like", (
        f"Expected fallback source 'binance_like', got '{state.cache.source}'"
    )
    assert not state.last_fetch_attempt_failed

    # Verify the Binance-like payload made it through.
    cfg = _make_bot_config("test_fallback")
    snapshot = await runtime.snapshot_for(cfg)
    assert "test_fallback" not in snapshot.stale
    assert snapshot.samples["test_fallback"]["source"] == "binance_like"

    await runtime.stop()


# ---------------------------------------------------------------------------
# Test: all-sources-fail → staleness in snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_sources_fail_produces_staleness() -> None:
    """When all sources fail and cache is old, the signal appears in stale."""
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    clock = FixedClock(t0)
    storage = _make_storage()

    # tolerance_seconds = 1 so the cached sample goes stale quickly.
    sig_cls = _make_signal_cls(
        "test_all_fail",
        cadence=100,
        tolerance=1,
        sources_list=[AlwaysFailSource],
    )

    reg = SignalRegistry()
    reg.register(sig_cls)

    import httpx

    runtime = SignalsRuntime(
        registry=reg,
        storage=storage,
        clock=clock,
        http=httpx.AsyncClient(),
    )
    runtime.subscribe("test_all_fail", {})
    await runtime.start()

    # Give the runner time to attempt (and fail) the first fetch.
    await asyncio.sleep(0.05)

    # No cache yet because all sources failed from the start.
    key = ("test_all_fail", next(iter(runtime._subs.values())).signal.params_hash)
    state = runtime._subs[key]
    assert state.cache is None, "Cache should be None when all sources always fail"
    assert state.last_fetch_attempt_failed

    # snapshot_for should mark the signal stale because cache is None.
    cfg = _make_bot_config("test_all_fail")
    snapshot: SignalSnapshot = await runtime.snapshot_for(cfg)
    assert "test_all_fail" in snapshot.stale

    await runtime.stop()


@pytest.mark.asyncio
async def test_cached_sample_goes_stale_after_tolerance() -> None:
    """A cached sample older than tolerance_seconds is marked stale."""
    # Seed a pre-existing cache entry by using a source that succeeds once
    # then fails on all subsequent calls.
    succeed_once_called = False

    class SucceedOnceThenFail(SignalSource):
        name: ClassVar[str] = "succeed_once"

        async def fetch(self, params: Mapping[str, Any]) -> Any:
            nonlocal succeed_once_called
            if not succeed_once_called:
                succeed_once_called = True
                return {"price": "50.0"}
            raise RuntimeError("down after first fetch")

    tolerance = 5  # seconds
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    clock = FixedClock(t0)
    storage = _make_storage()

    sig_cls = _make_signal_cls(
        "test_stale_after_tolerance",
        cadence=100,
        tolerance=tolerance,
        sources_list=[SucceedOnceThenFail],
    )

    reg = SignalRegistry()
    reg.register(sig_cls)

    import httpx

    runtime = SignalsRuntime(
        registry=reg,
        storage=storage,
        clock=clock,
        http=httpx.AsyncClient(),
    )
    runtime.subscribe("test_stale_after_tolerance", {})
    await runtime.start()

    # Let the first (successful) fetch run.
    await asyncio.sleep(0.05)

    key = (
        "test_stale_after_tolerance",
        next(iter(runtime._subs.values())).signal.params_hash,
    )
    state = runtime._subs[key]
    assert state.cache is not None, "Cache should have a value after the first fetch"

    # Not stale yet.
    cfg = _make_bot_config("test_stale_after_tolerance")
    snapshot = await runtime.snapshot_for(cfg)
    assert "test_stale_after_tolerance" not in snapshot.stale

    # Manually mark all-sources-failed and advance clock past tolerance.
    state.last_fetch_attempt_failed = True
    clock.advance(tolerance + 1)

    snapshot2 = await runtime.snapshot_for(cfg)
    assert "test_stale_after_tolerance" in snapshot2.stale

    await runtime.stop()
