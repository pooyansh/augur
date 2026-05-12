"""Unit tests for bot-side metric emission in BaseBot.run.

Tests:
- NullStrategy run for 3 ticks produces >= 2 BOT_TICK_LATENCY observations.
- ORDER_INTENT_TOTAL remains at 0 for all result labels (NullStrategy places nothing).
"""

from __future__ import annotations

import asyncio

import pytest

from tests.fixtures.null_strategy import make_null_bot


def _get_sample_count(metric_name: str, labels: dict[str, str]) -> float:
    """Extract the _count sample for a histogram metric from the REGISTRY."""
    from src.observability.metrics import REGISTRY

    for family in REGISTRY.collect():
        if family.name == metric_name:
            for sample in family.samples:
                if sample.name == f"{metric_name}_count" and all(
                    sample.labels.get(k) == v for k, v in labels.items()
                ):
                    return sample.value
    return 0.0


def _get_counter_value(metric_name: str, labels: dict[str, str]) -> float:
    """Extract a counter's total value from the REGISTRY."""
    from src.observability.metrics import REGISTRY

    for family in REGISTRY.collect():
        if family.name == metric_name or family.name == f"{metric_name}_total":
            for sample in family.samples:
                if "_total" in sample.name and all(
                    sample.labels.get(k) == v for k, v in labels.items()
                ):
                    return sample.value
    return 0.0


@pytest.mark.asyncio
async def test_null_strategy_tick_latency_observations() -> None:
    """NullStrategy run for 3 ticks produces latency observations via Prometheus."""
    from tests.fixtures.clocks import ManualClock

    bot_id = "null-metrics-test-001"
    clock = ManualClock()
    bot = make_null_bot(bot_id=bot_id, clock=clock)

    tick_count = 0

    original_on_tick = bot.on_tick

    async def counting_on_tick(signals):  # type: ignore[no-untyped-def]
        nonlocal tick_count
        result = await original_on_tick(signals)
        tick_count += 1
        if tick_count >= 3:
            raise asyncio.CancelledError()
        return result

    bot.on_tick = counting_on_tick  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await bot.run()

    # Ticks 1 and 2 complete fully (observe called after on_tick returns).
    # Tick 3 raises CancelledError from within on_tick before the observe call.
    # So we expect exactly 2 observations from the completed ticks.
    labels = {"bot_id": bot_id, "strategy": "null_strategy"}
    count = _get_sample_count("bot_tick_latency_seconds", labels)
    assert count >= 2, f"Expected at least 2 observations from completed ticks, got {count}"


@pytest.mark.asyncio
async def test_null_strategy_order_intent_total_is_zero() -> None:
    """NullStrategy produces no orders, so ORDER_INTENT_TOTAL stays at 0."""
    from tests.fixtures.clocks import ManualClock

    bot_id = "null-order-metrics-002"
    clock = ManualClock()
    bot = make_null_bot(bot_id=bot_id, clock=clock)

    tick_count = 0

    original_on_tick = bot.on_tick

    async def counting_on_tick(signals):  # type: ignore[no-untyped-def]
        nonlocal tick_count
        result = await original_on_tick(signals)
        tick_count += 1
        if tick_count >= 3:
            raise asyncio.CancelledError()
        return result

    bot.on_tick = counting_on_tick  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await bot.run()

    labels_base = {"bot_id": bot_id, "strategy": "null_strategy"}
    for result_label in ("accepted", "rejected", "risk_blocked", "kill_switch"):
        labels = {**labels_base, "result": result_label}
        val = _get_counter_value("order_intent_total", labels)
        assert val == 0.0, f"Expected 0 for result={result_label}, got {val}"
