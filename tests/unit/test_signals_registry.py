"""Tests for SignalRegistry auto-discovery and duplicate detection."""

from __future__ import annotations

import pytest
from src.signals.registry import SignalRegistry


def test_autodiscover_finds_btc_15min_and_election_polling() -> None:
    """autodiscover() registers both reference signals."""
    reg = SignalRegistry()
    reg.autodiscover("src.signals")
    names = reg.names
    assert "btc_15min" in names
    assert "election_polling" in names


def test_autodiscover_idempotent() -> None:
    """Calling autodiscover twice on the same registry is safe."""
    reg = SignalRegistry()
    reg.autodiscover("src.signals")
    # btc_15min and election_polling are the same class objects, so re-registering
    # is a no-op (the registry's idempotency guard kicks in).
    reg.autodiscover("src.signals")
    assert "btc_15min" in reg.names


def test_duplicate_registration_raises() -> None:
    """Registering two different classes with the same name raises ValueError."""
    from collections.abc import Mapping
    from typing import Any, ClassVar

    from src.signals.base import Signal, SignalSource

    class FakeSource(SignalSource):
        name: ClassVar[str] = "fake"

        async def fetch(self, params: Mapping[str, Any]) -> Any:
            return {}

    class SignalA(Signal):
        name: ClassVar[str] = "duplicate_test"
        cadence_seconds: ClassVar[int] = 60
        tolerance_seconds: ClassVar[int] = 120
        sources: ClassVar[list[type[SignalSource]]] = [FakeSource]

        def parse(self, source_name: str, raw: Any) -> Any:
            return raw

    class SignalB(Signal):
        name: ClassVar[str] = "duplicate_test"
        cadence_seconds: ClassVar[int] = 60
        tolerance_seconds: ClassVar[int] = 120
        sources: ClassVar[list[type[SignalSource]]] = [FakeSource]

        def parse(self, source_name: str, raw: Any) -> Any:
            return raw

    reg = SignalRegistry()
    reg.register(SignalA)
    with pytest.raises(ValueError, match="duplicate_test"):
        reg.register(SignalB)


def test_get_unknown_raises_key_error() -> None:
    """Getting an unregistered name raises KeyError with a useful message."""
    reg = SignalRegistry()
    with pytest.raises(KeyError, match="not found"):
        reg.get("nonexistent_signal")


def test_names_sorted() -> None:
    """names property returns a sorted list."""
    reg = SignalRegistry()
    reg.autodiscover("src.signals")
    names = reg.names
    assert names == sorted(names)
