"""Unit tests for supervisor spawn-time validators.

Covers ``validate_bot_signals`` (existing) and ``validate_bot_winning_rule``
(new), mirroring each other structurally per
``.claude/rules/08-signals.md`` / ``.claude/rules/10-winning-rules.md``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from src.manager.config import BotEntry, MarketRef, RiskOverride, SecretRef, WinningRuleRef
from src.manager.supervisor import validate_bot_signals, validate_bot_winning_rule


def _make_entry(**overrides: object) -> BotEntry:
    base: dict[str, object] = {
        "id": "test-bot-1",
        "strategy": "null_strategy",
        "market": MarketRef(exchange="polymarket", market_id="0xabc", token_id="123"),
        "mode": "paper",
        "schedule": "every:60s",
        "risk": RiskOverride(
            max_position_notional=Decimal("100"),
            max_daily_loss=Decimal("10"),
            max_orders_per_minute=5,
        ),
        "secrets": SecretRef(exchange_credentials="polymarket.dev"),
    }
    base.update(overrides)
    return BotEntry.model_validate(base)


# ---------------------------------------------------------------------------
# validate_bot_signals
# ---------------------------------------------------------------------------


def test_validate_bot_signals_unknown_name_raises() -> None:
    """An unregistered signal name raises ValueError listing available signals."""
    from src.manager.config import SignalSubscription

    entry = _make_entry(signals=[SignalSubscription(name="nonexistent_signal")])
    with pytest.raises(ValueError, match="nonexistent_signal"):
        validate_bot_signals(entry)


def test_validate_bot_signals_known_name_passes() -> None:
    """A registered signal name passes without raising."""
    from src.manager.config import SignalSubscription
    from src.signals.registry import signals as signal_registry

    signal_registry.autodiscover()
    entry = _make_entry(signals=[SignalSubscription(name="btc_15min")])
    validate_bot_signals(entry)  # must not raise


# ---------------------------------------------------------------------------
# validate_bot_winning_rule
# ---------------------------------------------------------------------------


def test_validate_bot_winning_rule_absent_is_noop() -> None:
    """No winning_rule configured -> validator is a no-op."""
    entry = _make_entry(winning_rule=None)
    validate_bot_winning_rule(entry)  # must not raise


def test_validate_bot_winning_rule_unknown_name_raises() -> None:
    """An unregistered winning rule name raises ValueError listing available rules."""
    entry = _make_entry(
        winning_rule=WinningRuleRef(name="polymarket.nonexistent_series.nonexistent_rule")
    )
    with pytest.raises(ValueError, match="nonexistent_rule"):
        validate_bot_winning_rule(entry)


def test_validate_bot_winning_rule_venue_mismatch_raises() -> None:
    """A rule whose venue prefix doesn't match the bot's market exchange raises."""
    from src.rules.registry import rules as rule_registry

    rule_registry.autodiscover()
    entry = _make_entry(
        market=MarketRef(exchange="echo", market_id="ECHO-1"),
        winning_rule=WinningRuleRef(name="polymarket.btc_up_or_down_5m.price_compare"),
    )
    with pytest.raises(ValueError, match="venue"):
        validate_bot_winning_rule(entry)


def test_validate_bot_winning_rule_valid_and_matching_passes() -> None:
    """A known rule whose venue matches the bot's market exchange passes."""
    from src.rules.registry import rules as rule_registry

    rule_registry.autodiscover()
    entry = _make_entry(
        market=MarketRef(exchange="polymarket", market_id="0xabc", token_id="123"),
        winning_rule=WinningRuleRef(name="polymarket.btc_up_or_down_5m.price_compare"),
    )
    validate_bot_winning_rule(entry)  # must not raise
