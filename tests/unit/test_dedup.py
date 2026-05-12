"""Tests for the alert deduplication logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.alerts.dedup import Deduper


def test_first_occurrence_not_suppressed() -> None:
    """A key seen for the first time must not be suppressed."""
    deduper = Deduper()
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert deduper.seen("key-abc", now) is False


def test_second_occurrence_within_window_suppressed() -> None:
    """The same key within 300 s must be suppressed."""
    deduper = Deduper()
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=299)

    assert deduper.seen("key-abc", t0) is False
    assert deduper.seen("key-abc", t1) is True


def test_occurrence_exactly_at_window_boundary_suppressed() -> None:
    """At exactly 299 seconds the key must still be suppressed."""
    deduper = Deduper()
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=299)

    deduper.seen("key-x", t0)
    assert deduper.seen("key-x", t1) is True


def test_occurrence_after_window_re_emitted() -> None:
    """After 300+ seconds the same key must not be suppressed (re-emit)."""
    deduper = Deduper()
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=301)

    assert deduper.seen("key-abc", t0) is False
    assert deduper.seen("key-abc", t1) is False  # window expired


def test_different_keys_are_independent() -> None:
    """Different keys must not interfere with each other."""
    deduper = Deduper()
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

    assert deduper.seen("key-a", t0) is False
    assert deduper.seen("key-b", t0) is False  # different key, must not be suppressed
    assert deduper.seen("key-a", t0) is True  # same key, suppressed


def test_clear_resets_state() -> None:
    """After clear(), all keys should behave as if never seen."""
    deduper = Deduper()
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=1)

    deduper.seen("key-abc", t0)
    deduper.clear()
    assert deduper.seen("key-abc", t1) is False  # cleared → fresh


def test_seen_updates_timestamp_after_window_expiry() -> None:
    """After the window expires and the key is re-sent, the timestamp resets."""
    deduper = Deduper()
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=301)  # expired
    t2 = t1 + timedelta(seconds=10)  # within new window

    deduper.seen("key-abc", t0)  # first send
    deduper.seen("key-abc", t1)  # re-send after expiry — resets timestamp
    assert deduper.seen("key-abc", t2) is True  # suppressed again from t1
