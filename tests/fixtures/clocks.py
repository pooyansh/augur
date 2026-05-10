"""Injectable clocks for deterministic tests."""

from __future__ import annotations

__all__ = ["ManualClock"]

from datetime import UTC, datetime

from src.bots.base import Clock


class ManualClock(Clock):
    """A clock whose ``now()`` is controlled by the test.

    Args:
        start: Initial UTC datetime.  Defaults to Unix epoch.

    Example::

        clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        clock.advance(60)   # advance 60 seconds
        assert clock.now().second == 0
    """

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 1, 1, tzinfo=UTC)
        self._now = start

    def now(self) -> datetime:
        """Return the current (manually controlled) datetime."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Advance the clock by ``seconds``.

        Args:
            seconds: Number of seconds to advance (may be fractional).
        """
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)

    def set(self, dt: datetime) -> None:
        """Set the clock to an absolute datetime.

        Args:
            dt: New current time (must be timezone-aware).
        """
        self._now = dt
