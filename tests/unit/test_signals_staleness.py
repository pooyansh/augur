"""Pure-function tests for staleness utilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from src.signals.staleness import is_stale, merge_staleness

# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "age_s, tolerance_s, expected",
    [
        (0, 60, False),  # brand new
        (59, 60, False),  # under tolerance
        (60, 60, False),  # exactly at tolerance — NOT stale (strictly >)
        (61, 60, True),  # one second over
        (3600, 60, True),  # very old
    ],
)
def test_is_stale_boundary_conditions(age_s: int, tolerance_s: int, expected: bool) -> None:
    """is_stale uses strict greater-than comparison against tolerance."""
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    observed_at = now - timedelta(seconds=age_s)
    assert is_stale(observed_at, tolerance_s, now) == expected


def test_is_stale_with_future_observed_at_not_stale() -> None:
    """A sample observed in the future (clock skew) is never stale."""
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    observed_at = now + timedelta(seconds=10)
    assert not is_stale(observed_at, 60, now)


# ---------------------------------------------------------------------------
# merge_staleness
# ---------------------------------------------------------------------------


def test_merge_staleness_empty() -> None:
    """Empty mapping produces empty frozenset."""
    result = merge_staleness({})
    assert result == frozenset()


def test_merge_staleness_all_fresh() -> None:
    """All False flags → empty frozenset."""
    result = merge_staleness({"a": False, "b": False})
    assert result == frozenset()


def test_merge_staleness_some_stale() -> None:
    """Only stale names are included."""
    result = merge_staleness({"a": True, "b": False, "c": True})
    assert result == frozenset({"a", "c"})


def test_merge_staleness_all_stale() -> None:
    """All True → all names returned."""
    result = merge_staleness({"x": True, "y": True})
    assert result == frozenset({"x", "y"})


def test_merge_staleness_returns_frozenset() -> None:
    """Result is always a frozenset (immutable)."""
    result = merge_staleness({"a": True})
    assert isinstance(result, frozenset)
