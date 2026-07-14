"""Tests for WinningRuleRegistry auto-discovery and duplicate detection.

Mirrors ``tests/unit/test_signals_registry.py`` structurally.
"""

from __future__ import annotations

import pytest
from src.rules.registry import WinningRuleRegistry


def test_autodiscover_finds_price_compare_under_dotted_name() -> None:
    """autodiscover() registers the reference rule under its fully-qualified dotted name."""
    reg = WinningRuleRegistry()
    reg.autodiscover("src.rules")
    assert "polymarket.btc_up_or_down_5m.price_compare" in reg.names


def test_autodiscover_idempotent() -> None:
    """Calling autodiscover twice on the same registry is safe."""
    reg = WinningRuleRegistry()
    reg.autodiscover("src.rules")
    reg.autodiscover("src.rules")
    assert "polymarket.btc_up_or_down_5m.price_compare" in reg.names


def test_duplicate_registration_raises() -> None:
    """Registering two different classes with the same dotted name raises ValueError."""
    from typing import ClassVar

    from src.rules.base import ProvisionalRuling, WinningRule, WinningRuleContext

    class RuleA(WinningRule):
        name: ClassVar[str] = "polymarket.duplicate_series.dup_rule"

        def evaluate(self, ctx: WinningRuleContext) -> ProvisionalRuling:
            return ProvisionalRuling.UNDECIDED

    class RuleB(WinningRule):
        name: ClassVar[str] = "polymarket.duplicate_series.dup_rule"

        def evaluate(self, ctx: WinningRuleContext) -> ProvisionalRuling:
            return ProvisionalRuling.UNDECIDED

    reg = WinningRuleRegistry()
    reg.register(RuleA)
    with pytest.raises(ValueError, match=r"polymarket\.duplicate_series\.dup_rule"):
        reg.register(RuleB)


def test_get_unknown_raises_key_error() -> None:
    """Getting an unregistered name raises KeyError with a useful message."""
    reg = WinningRuleRegistry()
    with pytest.raises(KeyError, match="not found"):
        reg.get("polymarket.nonexistent_series.nonexistent_rule")


def test_names_sorted() -> None:
    """names property returns a sorted list."""
    reg = WinningRuleRegistry()
    reg.autodiscover("src.rules")
    names = reg.names
    assert names == sorted(names)
