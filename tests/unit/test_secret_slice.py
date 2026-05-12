"""Tests for Secrets.slice_for dotted-path resolution."""

from __future__ import annotations

import pytest
from src.secrets.loader import Secrets


def _make_secrets() -> Secrets:
    data = {
        "exchanges": {
            "polymarket": {
                "disposable": {
                    "private_key": "pk-test-001",
                    "api_key": "ak-test-001",
                },
                "hot_wallet": {
                    "address": "0xABC123",
                },
            },
            "kalshi": {
                "api_key": "kalshi-key-001",
            },
        },
        "alerts": {
            "slack": {"webhook_url": "https://hooks.slack.com/test"},
        },
    }
    return Secrets(data)


def test_slice_for_returns_nested_dict() -> None:
    """Dotted path to a nested dict returns the dict."""
    secrets = _make_secrets()
    result = secrets.slice_for("exchanges.polymarket.disposable")
    assert result == {"private_key": "pk-test-001", "api_key": "ak-test-001"}


def test_slice_for_returns_leaf_string() -> None:
    """Dotted path to a leaf string returns the string."""
    secrets = _make_secrets()
    result = secrets.slice_for("exchanges.kalshi.api_key")
    assert result == "kalshi-key-001"


def test_slice_for_returns_top_level_dict() -> None:
    """Single segment (no dots) returns the file-level dict."""
    secrets = _make_secrets()
    result = secrets.slice_for("alerts")
    assert "slack" in result  # type: ignore[operator]


def test_slice_for_missing_first_segment_raises_key_error() -> None:
    """A missing top-level segment must raise KeyError."""
    secrets = _make_secrets()
    with pytest.raises(KeyError):
        secrets.slice_for("nonexistent.path")


def test_slice_for_missing_nested_segment_raises_key_error() -> None:
    """A missing nested segment must raise KeyError."""
    secrets = _make_secrets()
    with pytest.raises(KeyError):
        secrets.slice_for("exchanges.polymarket.nonexistent")


def test_slice_for_traversal_into_non_dict_raises_key_error() -> None:
    """Attempting to traverse into a non-dict raises KeyError."""
    secrets = _make_secrets()
    with pytest.raises(KeyError):
        secrets.slice_for("exchanges.kalshi.api_key.subkey")


def test_slice_for_accepts_secret_ref_object() -> None:
    """SecretRef-like objects (with .exchange_credentials) are accepted."""
    secrets = _make_secrets()

    class FakeSecretRef:
        exchange_credentials = "exchanges.polymarket.hot_wallet"

    result = secrets.slice_for(FakeSecretRef())
    assert result == {"address": "0xABC123"}


def test_raw_returns_full_data() -> None:
    """Secrets.raw() returns the full underlying mapping."""
    secrets = _make_secrets()
    raw = secrets.raw()
    assert "exchanges" in raw
    assert "alerts" in raw
