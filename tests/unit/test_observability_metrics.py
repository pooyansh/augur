"""Unit tests for src/observability/metrics.py.

Tests:
- Observing a value into BOT_TICK_LATENCY appears in render_metrics() output.
- No metric in the registry uses market_id as a label (cardinality rule).
"""

from __future__ import annotations


def test_bot_tick_latency_appears_in_render() -> None:
    """After observing a latency value, render_metrics includes the series."""
    from src.observability.metrics import BOT_TICK_LATENCY, render_metrics

    BOT_TICK_LATENCY.labels(bot_id="test-b1", strategy="test-s1").observe(0.5)

    body, content_type = render_metrics()
    text = body.decode("utf-8")

    assert "bot_tick_latency_seconds" in text
    assert "test-b1" in text
    assert "test-s1" in text
    assert "prometheus" in content_type.lower() or "text" in content_type.lower()


def test_cardinality_no_market_id_label() -> None:
    """No metric in the REGISTRY has market_id as a label dimension."""
    from src.observability.metrics import REGISTRY

    for metric in REGISTRY.collect():
        for sample in metric.samples:
            label_names = list(sample.labels.keys())
            assert "market_id" not in label_names, (
                f"Metric '{metric.name}' has forbidden label 'market_id'. "
                "Per .claude/rules/09-observability.md, market_id is NOT a label."
            )


def test_render_metrics_returns_bytes_and_content_type() -> None:
    """render_metrics returns (bytes, str) with correct types."""
    from src.observability.metrics import render_metrics

    body, content_type = render_metrics()
    assert isinstance(body, bytes)
    assert isinstance(content_type, str)
    assert len(body) > 0
