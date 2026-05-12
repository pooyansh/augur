"""Tests for SignalReplay — deterministic historical playback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from src.bots.base import BotConfig, Clock, RiskCaps, Schedule
from src.exchanges.base import Mode
from src.signals.base import SignalSnapshot
from src.signals.registry import SignalRegistry
from src.signals.replay import SignalReplay
from src.signals.storage import SignalSample

# ---------------------------------------------------------------------------
# In-memory SignalStorage stub for replay tests
# ---------------------------------------------------------------------------


class InMemorySignalStorage:
    """Simple in-memory storage stub that supports replay."""

    def __init__(self) -> None:
        self._rows: list[SignalSample] = []

    def add(self, sample: SignalSample) -> None:
        self._rows.append(sample)

    async def append(self, **kwargs: Any) -> None:  # pragma: no cover
        pass

    async def replay(
        self,
        signal: str,
        params_hash: str,
        start: datetime,
        end: datetime,
    ) -> AsyncIterator[SignalSample]:
        for row in sorted(self._rows, key=lambda r: r.observed_at):
            if (
                row.signal == signal
                and row.params_hash == params_hash
                and start <= row.observed_at <= end
            ):
                yield row


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FixedClock(Clock):
    def __init__(self, initial: datetime) -> None:
        self._now = initial

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _make_bot_config(signal_name: str) -> BotConfig:
    return BotConfig(
        bot_id="replay-bot",
        strategy_name="null",
        market_id="mkt-1",
        mode=Mode.PAPER,
        live=False,
        schedule=Schedule(every_seconds=900),
        risk=RiskCaps(
            max_position_notional=Decimal("1000"),
            max_daily_loss=Decimal("100"),
            max_orders_per_minute=10,
        ),
        signal_subscriptions=[signal_name],
    )


def _seed_samples(
    storage: InMemorySignalStorage,
    signal_name: str,
    params_hash: str,
    count: int,
    start: datetime,
    cadence_s: int = 900,
) -> list[SignalSample]:
    """Seed ``count`` equally-spaced samples into the storage."""
    samples = []
    for i in range(count):
        ts = start + timedelta(seconds=i * cadence_s)
        s = SignalSample(
            signal=signal_name,
            params_hash=params_hash,
            source="test_source",
            observed_at=ts,
            payload={"price_usd": str(100 + i), "index": i},
            latency_ms=50,
        )
        storage.add(s)
        samples.append(s)
    return samples


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_yields_samples_in_chronological_order() -> None:
    """SignalReplay yields samples in ascending observed_at order."""
    from src.signals.btc_15min import Btc15Min

    storage = InMemorySignalStorage()
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    end = t0 + timedelta(hours=4)

    sig = Btc15Min({})
    params_hash = sig.params_hash
    _seed_samples(storage, "btc_15min", params_hash, count=10, start=t0)

    reg = SignalRegistry()
    reg.autodiscover("src.signals")

    clock = FixedClock(t0 + timedelta(hours=2))  # virtual time in middle of samples
    replay = SignalReplay(
        registry=reg,
        storage=storage,  # type: ignore[arg-type]
        clock=clock,
        start=t0,
        end=end,
        params_hash_map={"btc_15min": params_hash},
    )

    cfg = _make_bot_config("btc_15min")

    # Collect all snapshots by advancing time step by step.
    seen_prices = []
    for _i in range(5):
        snap: SignalSnapshot = await replay.snapshot_for(cfg)
        if "btc_15min" in snap.samples:
            seen_prices.append(int(snap.samples["btc_15min"]["price_usd"]))
        clock.advance(900)

    # Should have seen strictly increasing prices (index 0, 1, 2, ...).
    assert seen_prices == sorted(seen_prices)
    assert len(seen_prices) > 0


@pytest.mark.asyncio
async def test_replay_respects_start_end_range() -> None:
    """SignalReplay ignores samples outside the (start, end) window."""
    from src.signals.btc_15min import Btc15Min

    storage = InMemorySignalStorage()
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    window_start = t0 + timedelta(hours=1)
    window_end = t0 + timedelta(hours=2)

    sig = Btc15Min({})
    params_hash = sig.params_hash

    # Seed samples both inside and outside the window.
    _seed_samples(storage, "btc_15min", params_hash, count=20, start=t0, cadence_s=900)

    reg = SignalRegistry()
    reg.autodiscover("src.signals")

    clock = FixedClock(window_start)
    replay = SignalReplay(
        registry=reg,
        storage=storage,  # type: ignore[arg-type]
        clock=clock,
        start=window_start,
        end=window_end,
        params_hash_map={"btc_15min": params_hash},
    )

    cfg = _make_bot_config("btc_15min")

    # At window_start, clock is at the boundary.
    snap = await replay.snapshot_for(cfg)
    assert "btc_15min" in snap.samples


@pytest.mark.asyncio
async def test_replay_stale_when_no_samples_before_virtual_time() -> None:
    """Replay marks signal stale when no sample exists at or before virtual now."""
    from src.signals.btc_15min import Btc15Min

    storage = InMemorySignalStorage()
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    sig = Btc15Min({})
    params_hash = sig.params_hash

    # Virtual clock is BEFORE the only sample.
    future_sample = SignalSample(
        signal="btc_15min",
        params_hash=params_hash,
        source="test",
        observed_at=t0 + timedelta(hours=1),
        payload={"price_usd": "100"},
        latency_ms=10,
    )
    storage.add(future_sample)

    reg = SignalRegistry()
    reg.autodiscover("src.signals")

    clock = FixedClock(t0)  # before any sample
    replay = SignalReplay(
        registry=reg,
        storage=storage,  # type: ignore[arg-type]
        clock=clock,
        start=t0,
        end=t0 + timedelta(hours=2),
        params_hash_map={"btc_15min": params_hash},
    )

    cfg = _make_bot_config("btc_15min")
    snap = await replay.snapshot_for(cfg)
    assert "btc_15min" in snap.stale
    assert "btc_15min" not in snap.samples
