"""Election polling signal — stub implementation.

This module exists to prove the ``Signal`` ABC is not BTC-shaped.  The
``ElectionPolling`` signal has:
- A daily cadence (86 400 s) vs. BTC's 15-min cadence.
- A multi-candidate payload shape instead of a single scalar price.
- A ``MultiCandidateSource`` ABC that any real polling data vendor would
  implement.

No real upstream source is wired in — the stub ``NoOpPollingSource`` always
raises, leaving the runner with an empty cache and the signal perpetually stale.
Strategies that subscribe to this signal will see it in ``snapshot.stale``
immediately.

To add a real source: subclass ``MultiCandidateSource``, implement ``fetch``,
and add the class to ``ElectionPolling.sources``.
"""

from __future__ import annotations

__all__ = ["ElectionPolling", "MultiCandidateSource", "NoOpPollingSource"]

import logging
from abc import abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

from src.signals.base import Signal, SignalSource
from src.signals.registry import signal

logger = logging.getLogger(__name__)


class MultiCandidateSource(SignalSource):
    """Abstract base for election polling data sources.

    Implementations must return a list of candidate support records::

        [
            {"candidate": "Alice", "support_pct": 52.3},
            {"candidate": "Bob",   "support_pct": 44.1},
        ]

    The list may include any number of candidates.  ``support_pct`` values are
    floats (0-100).
    """

    @abstractmethod
    async def fetch(self, params: Mapping[str, Any]) -> Any:
        """Fetch the latest polling averages.

        Args:
            params: May include ``{"race": "US_PRES_2028"}`` or similar
                race identifiers.

        Returns:
            Source-specific raw response — anything parseable by
            :meth:`ElectionPolling.parse`.
        """


class NoOpPollingSource(MultiCandidateSource):
    """Stub source — always raises to exercise the all-sources-fail path.

    Replace this with a real vendor implementation when a polling feed is
    available.
    """

    name: ClassVar[str] = "noop"

    async def fetch(self, params: Mapping[str, Any]) -> Any:
        """Always raises — no real polling source is configured.

        Args:
            params: Ignored.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "ElectionPolling has no real source configured.  "
            "Subclass MultiCandidateSource and add it to ElectionPolling.sources."
        )


@signal
class ElectionPolling(Signal):
    """Daily election polling signal (stub).

    Cadence: 86 400 s (once per day).  Freshness tolerance: 172 800 s (48 h).

    Canonical payload shape::

        {
            "race": "US_PRES_2028",   # from params["race"] or "unknown"
            "candidates": [
                {"candidate": "Alice", "support_pct": 52.3},
                {"candidate": "Bob",   "support_pct": 44.1},
            ],
            "source": "noop",
        }

    Since ``NoOpPollingSource`` always fails, this signal will always be in
    ``snapshot.stale`` until a real source is added.
    """

    name: ClassVar[str] = "election_polling"
    cadence_seconds: ClassVar[int] = 86_400
    tolerance_seconds: ClassVar[int] = 172_800
    sources: ClassVar[list[type[SignalSource]]] = [NoOpPollingSource]

    def parse(self, source_name: str, raw: Any) -> dict[str, Any]:
        """Parse a raw polling response into the canonical shape.

        Args:
            source_name: Name of the source (e.g. ``"noop"``).
            raw: Source-specific raw response (list of candidate dicts).

        Returns:
            Canonical dict with ``race``, ``candidates``, and ``source``.

        Raises:
            ValueError: If ``raw`` is not a list of candidate records.
        """
        if not isinstance(raw, list):
            raise ValueError(
                f"ElectionPolling expects a list of candidate records, got {type(raw)}"
            )
        for item in raw:
            if not isinstance(item, dict) or "candidate" not in item or "support_pct" not in item:
                raise ValueError(f"Invalid candidate record: {item!r}")

        race = str(self._params.get("race", "unknown"))
        return {
            "race": race,
            "candidates": raw,
            "source": source_name,
        }
