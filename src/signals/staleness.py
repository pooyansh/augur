"""Pure staleness-check functions for the signals layer.

Staleness is data, not an exception (see .claude/rules/08-signals.md).
These functions are pure (no I/O, no side effects) so they can be used
in both the runner and tests without any setup.
"""

from __future__ import annotations

__all__ = ["is_stale", "merge_staleness"]

from collections.abc import Mapping
from datetime import datetime


def is_stale(observed_at: datetime, tolerance_s: int, now: datetime) -> bool:
    """Return ``True`` if a cached sample has exceeded its freshness tolerance.

    Args:
        observed_at: UTC datetime when the sample was recorded.
        tolerance_s: Maximum acceptable age in seconds before the signal is
            considered stale (``Signal.tolerance_seconds``).
        now: Current UTC datetime (injectable for deterministic tests).

    Returns:
        ``True`` if ``(now - observed_at).total_seconds() > tolerance_s``.
    """
    age = (now - observed_at).total_seconds()
    return age > tolerance_s


def merge_staleness(per_signal_stale: Mapping[str, bool]) -> frozenset[str]:
    """Collect all stale signal names into an immutable set.

    Args:
        per_signal_stale: Mapping of signal name to its staleness flag.

    Returns:
        Frozenset of signal names where the flag is ``True``.
    """
    return frozenset(name for name, stale in per_signal_stale.items() if stale)
