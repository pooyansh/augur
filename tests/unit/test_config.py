"""Unit tests for the BotsRoster Pydantic schema (src/manager/config.py).

Tests cover:
- Valid YAML is accepted without errors.
- Each validation rule fires with a clear message for the right input.
- schedule_seconds() parses correctly.
- Cron syntax raises NotImplementedError (not yet implemented).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError
from src.manager.config import BotsRoster

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(**overrides: object) -> dict[object, object]:
    """Build a valid BotEntry dict with optional overrides."""
    base: dict[object, object] = {
        "id": "echo-paper-1",
        "strategy": "null_strategy",
        "market": {"exchange": "echo", "market_id": "ECHO-TEST"},
        "mode": "paper",
        "schedule": "every:60s",
        "risk": {
            "max_position_notional": "100",
            "max_daily_loss": "10",
            "max_orders_per_minute": 5,
        },
        "secrets": {"exchange_credentials": "echo.dev"},
    }
    base.update(overrides)
    return base


def _make_roster(entries: list[dict[object, object]]) -> dict[object, object]:
    return {"bots": entries}


# ---------------------------------------------------------------------------
# Valid schema acceptance
# ---------------------------------------------------------------------------


def test_valid_entry_accepted() -> None:
    """A fully-specified valid entry validates without errors."""
    data = _make_roster([_make_entry()])
    roster = BotsRoster.model_validate(data)
    assert len(roster.bots) == 1
    entry = roster.bots[0]
    assert entry.id == "echo-paper-1"
    assert entry.strategy == "null_strategy"
    assert entry.mode == "paper"
    assert entry.schedule_seconds() == 60


def test_empty_roster_accepted() -> None:
    """An empty bots list is valid — zero bots spawned."""
    roster = BotsRoster.model_validate({"bots": []})
    assert roster.bots == []


def test_multiple_bots_with_unique_ids() -> None:
    """Multiple entries with distinct ids are accepted."""
    data = _make_roster(
        [
            _make_entry(id="bot-a"),
            _make_entry(id="bot-b"),
        ]
    )
    roster = BotsRoster.model_validate(data)
    assert [e.id for e in roster.bots] == ["bot-a", "bot-b"]


def test_default_mode_is_paper() -> None:
    """Omitting ``mode`` defaults to 'paper'."""
    entry_data = _make_entry()
    del entry_data["mode"]  # type: ignore[arg-type]
    roster = BotsRoster.model_validate(_make_roster([entry_data]))
    assert roster.bots[0].mode == "paper"


def test_signals_default_to_empty_list() -> None:
    """Omitting ``signals`` defaults to an empty list."""
    roster = BotsRoster.model_validate(_make_roster([_make_entry()]))
    assert roster.bots[0].signals == []


def test_risk_caps_parsed_as_decimal() -> None:
    """Risk cap values are coerced to Decimal."""
    roster = BotsRoster.model_validate(_make_roster([_make_entry()]))
    risk = roster.bots[0].risk
    assert risk.max_position_notional == Decimal("100")
    assert risk.max_daily_loss == Decimal("10")
    assert risk.max_orders_per_minute == 5


# ---------------------------------------------------------------------------
# Duplicate id rejection
# ---------------------------------------------------------------------------


def test_duplicate_bot_ids_rejected() -> None:
    """Duplicate bot ids in the same roster fail validation with a clear message."""
    data = _make_roster(
        [
            _make_entry(id="same-id"),
            _make_entry(id="same-id"),
        ]
    )
    with pytest.raises(ValidationError) as exc_info:
        BotsRoster.model_validate(data)
    assert "same-id" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bot id pattern validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "ab",  # too short (< 3 chars)
        "AB",  # uppercase not allowed
        "with space",  # space not allowed
        "with.dot",  # dot not allowed
        "a" * 65,  # too long (> 64 chars)
    ],
)
def test_invalid_bot_id_rejected(bad_id: str) -> None:
    """Bot ids must match ``^[a-z0-9_-]{3,64}$``."""
    data = _make_roster([_make_entry(id=bad_id)])
    with pytest.raises(ValidationError):
        BotsRoster.model_validate(data)


@pytest.mark.parametrize(
    "good_id",
    [
        "abc",
        "echo-paper-1",
        "bot_123",
        "a" * 64,
        "123",
    ],
)
def test_valid_bot_ids_accepted(good_id: str) -> None:
    """Valid bot id patterns are accepted."""
    data = _make_roster([_make_entry(id=good_id)])
    roster = BotsRoster.model_validate(data)
    assert roster.bots[0].id == good_id


# ---------------------------------------------------------------------------
# Schedule validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schedule,expected_s",
    [
        ("every:60s", 60),
        ("every:1s", 1),
        ("every:900s", 900),
        ("every:3600s", 3600),
    ],
)
def test_valid_schedule_strings(schedule: str, expected_s: int) -> None:
    """Valid 'every:Ns' schedules parse correctly."""
    data = _make_roster([_make_entry(schedule=schedule)])
    roster = BotsRoster.model_validate(data)
    assert roster.bots[0].schedule_seconds() == expected_s


@pytest.mark.parametrize(
    "bad_schedule",
    [
        "every:0s",  # zero interval
        "every:-10s",  # negative interval — won't match pattern
        "60s",  # missing 'every:' prefix
        "every:60",  # missing 's' suffix
        "*/5 * * * *",  # cron — not yet supported (NotImplementedError)
        "@hourly",  # cron alias — not yet supported
        "garbage",  # totally invalid
    ],
)
def test_invalid_schedule_strings_rejected(bad_schedule: str) -> None:
    """Invalid or unsupported schedule strings fail validation."""
    data = _make_roster([_make_entry(schedule=bad_schedule)])
    with pytest.raises((ValidationError, NotImplementedError)):
        BotsRoster.model_validate(data)


# ---------------------------------------------------------------------------
# Exchange validation
# ---------------------------------------------------------------------------


def test_valid_exchanges_accepted() -> None:
    """All supported exchange values are accepted."""
    for exchange in ("polymarket", "kalshi", "echo"):
        data = _make_roster([_make_entry(market={"exchange": exchange, "market_id": "TEST"})])
        roster = BotsRoster.model_validate(data)
        assert roster.bots[0].market.exchange == exchange


def test_unknown_exchange_rejected() -> None:
    """An unrecognised exchange value fails validation."""
    data = _make_roster([_make_entry(market={"exchange": "unknown_venue", "market_id": "TEST"})])
    with pytest.raises(ValidationError):
        BotsRoster.model_validate(data)


# ---------------------------------------------------------------------------
# Risk cap validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("max_position_notional", "-1"),
        ("max_position_notional", "0"),
        ("max_daily_loss", "0"),
        ("max_orders_per_minute", 0),
        ("max_orders_per_minute", -5),
    ],
)
def test_non_positive_risk_caps_rejected(field: str, bad_value: object) -> None:
    """Risk caps must all be strictly positive."""
    risk = {
        "max_position_notional": "100",
        "max_daily_loss": "10",
        "max_orders_per_minute": 5,
    }
    risk[field] = bad_value  # type: ignore[index]
    data = _make_roster([_make_entry(risk=risk)])
    with pytest.raises(ValidationError):
        BotsRoster.model_validate(data)


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_field", ["id", "strategy", "market", "risk", "secrets"])
def test_missing_required_fields_rejected(missing_field: str) -> None:
    """Omitting any required field fails validation."""
    entry = _make_entry()
    del entry[missing_field]  # type: ignore[arg-type]
    data = _make_roster([entry])
    with pytest.raises(ValidationError):
        BotsRoster.model_validate(data)


# ---------------------------------------------------------------------------
# Mode validation
# ---------------------------------------------------------------------------


def test_live_mode_accepted() -> None:
    """'live' is a valid mode value."""
    data = _make_roster([_make_entry(mode="live")])
    roster = BotsRoster.model_validate(data)
    assert roster.bots[0].mode == "live"


def test_invalid_mode_rejected() -> None:
    """An unrecognised mode value fails validation."""
    data = _make_roster([_make_entry(mode="sandbox")])
    with pytest.raises(ValidationError):
        BotsRoster.model_validate(data)
