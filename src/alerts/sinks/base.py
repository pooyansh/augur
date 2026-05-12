"""Abstract sink base class."""

from __future__ import annotations

__all__ = ["Sink"]

from abc import ABC, abstractmethod


class Sink(ABC):
    """Abstract base for all alerting sinks.

    Each concrete sink (Slack, Discord, Telegram) implements :meth:`send`.
    Failures in :meth:`send` MUST NOT propagate to callers — the router
    catches them and logs at ``warning``.

    Args:
        name: Human-readable sink name used in log messages.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def send(self, message: str) -> None:
        """Deliver ``message`` to the sink.

        Args:
            message: Redacted, plain-text alert body.

        Raises:
            Exception: Callers should catch all exceptions — the router
                handles them as best-effort failures.
        """
