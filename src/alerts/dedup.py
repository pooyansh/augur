"""In-memory deduplication for alert messages.

Alerts with the same dedup key are suppressed for 300 seconds (5 minutes)
after the first send.  This prevents spam when a bot flaps repeatedly.

The dedup key is assembled by the :class:`~src.alerts.router.AlertRouter`
using ``blake2s``.  The :class:`Deduper` only sees the opaque hex string.

No persistence — dedup state resets on process restart.  This is acceptable
because the 300-second window is shorter than any expected process restart
cycle and it is better to over-alert than under-alert.
"""

from __future__ import annotations

__all__ = ["Deduper"]

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Suppression window in seconds.
_SUPPRESS_WINDOW_S: float = 300.0


class Deduper:
    """Suppresses duplicate alert sends within a rolling time window.

    Thread-safe in the sense that asyncio runs on a single event-loop thread;
    no additional locking is needed.
    """

    def __init__(self) -> None:
        # key -> last-seen UTC datetime
        self._seen: dict[str, datetime] = {}

    def seen(self, key: str, now: datetime | None = None) -> bool:
        """Return ``True`` and update state if the key was seen recently.

        If ``True`` is returned, the caller must suppress the send.
        If ``False`` is returned, the caller proceeds with the send and the
        key is recorded with the current timestamp.

        Args:
            key: Opaque hex dedup key.
            now: Reference UTC datetime.  Defaults to ``datetime.now(UTC)``.

        Returns:
            ``True`` if the key was seen within the suppression window;
            ``False`` if this is a new key or the window has expired.
        """
        if now is None:
            now = datetime.now(tz=UTC)

        last = self._seen.get(key)
        if last is not None:
            age_s = (now - last).total_seconds()
            if age_s < _SUPPRESS_WINDOW_S:
                logger.debug(
                    "Alert deduped: key=%s age=%.1fs (window=%.0fs)",
                    key,
                    age_s,
                    _SUPPRESS_WINDOW_S,
                )
                return True

        # Not seen recently — record and return False.
        self._seen[key] = now
        return False

    def clear(self) -> None:
        """Clear all dedup state (test helper)."""
        self._seen.clear()
