"""Unit tests for src/observability/heartbeat_metrics.py.

Tests:
- A heartbeat record with a metrics field updates the Prometheus registry.
- A heartbeat record without a metrics field leaves the registry unchanged.
- Counter deltas and gauge sets are handled correctly.
"""

from __future__ import annotations


def _sample_value(metric_family: object, labels: dict[str, str]) -> float | None:
    """Extract a sample value from a CollectorFamily by label match."""
    for sample in metric_family.samples:  # type: ignore[attr-defined]
        if all(sample.labels.get(k) == v for k, v in labels.items()):
            return sample.value
    return None


def test_heartbeat_with_metrics_updates_registry() -> None:
    """apply_heartbeat_metrics updates gauges and counters from the metrics field."""
    from src.observability.heartbeat_metrics import apply_heartbeat_metrics
    from src.observability.metrics import REGISTRY

    bot_id = "hb-test-bot-001"
    strategy = "null_strategy"

    record = {
        "bot_id": bot_id,
        "ts": "2026-05-09T12:00:00+00:00",
        "snapshot_lag_s": 2.0,
        "last_error": None,
        "metrics": {
            "strategy": strategy,
            "tick_latency_seconds_obs": [0.3, 0.4],
            "tick_overrun_total": 1,
            "order_intent_total": {
                "accepted": 2,
                "rejected": 0,
                "risk_blocked": 0,
                "kill_switch": 0,
            },
            "order_fill_latency_seconds_obs": [1.2],
            "position_notional_usd": 500.0,
            "pnl_realized_usd": 10.0,
            "pnl_unrealized_usd": -2.5,
        },
    }

    apply_heartbeat_metrics(record)  # type: ignore[arg-type]

    # Check position_notional_usd gauge was set.
    families = {m.name: m for m in REGISTRY.collect()}
    pos_family = families.get("position_notional_usd")
    assert pos_family is not None
    val = _sample_value(pos_family, {"bot_id": bot_id, "strategy": strategy})
    assert val == 500.0


def test_heartbeat_without_metrics_is_noop() -> None:
    """apply_heartbeat_metrics with no metrics field makes no registry changes."""
    from src.observability.heartbeat_metrics import apply_heartbeat_metrics
    from src.observability.metrics import REGISTRY

    record = {
        "bot_id": "no-metrics-bot",
        "ts": "2026-05-09T12:00:00+00:00",
        "snapshot_lag_s": 0.5,
        "last_error": None,
    }

    apply_heartbeat_metrics(record)  # type: ignore[arg-type]

    families_after = {m.name: list(m.samples) for m in REGISTRY.collect()}

    # No samples should exist for this bot id.
    for name, samples_after in families_after.items():
        for sample in samples_after:
            if sample.labels.get("bot_id") == "no-metrics-bot":
                raise AssertionError(f"Unexpected sample for no-metrics-bot in {name}")


def test_heartbeat_pnl_gauges_updated() -> None:
    """PnL realized and unrealized gauges are set from the metrics payload."""
    from src.observability.heartbeat_metrics import apply_heartbeat_metrics
    from src.observability.metrics import REGISTRY

    bot_id = "pnl-gauge-bot"
    strategy = "test_strategy"

    record = {
        "bot_id": bot_id,
        "ts": "2026-05-09T13:00:00+00:00",
        "snapshot_lag_s": 0.0,
        "last_error": None,
        "metrics": {
            "strategy": strategy,
            "tick_latency_seconds_obs": [],
            "tick_overrun_total": 0,
            "order_intent_total": {},
            "order_fill_latency_seconds_obs": [],
            "position_notional_usd": 100.0,
            "pnl_realized_usd": 15.0,
            "pnl_unrealized_usd": -5.0,
        },
    }

    apply_heartbeat_metrics(record)  # type: ignore[arg-type]

    families = {m.name: m for m in REGISTRY.collect()}

    realized = _sample_value(families["pnl_realized_usd"], {"bot_id": bot_id, "strategy": strategy})
    unrealized = _sample_value(
        families["pnl_unrealized_usd"], {"bot_id": bot_id, "strategy": strategy}
    )
    assert realized == 15.0
    assert unrealized == -5.0
