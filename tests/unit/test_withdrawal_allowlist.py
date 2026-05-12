"""Tests for WithdrawalAllowlist."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.risk.withdrawal_allowlist import WithdrawalAllowlist


def test_is_allowed_exact_match() -> None:
    """An address that is in the allowlist must be permitted."""
    al = WithdrawalAllowlist(frozenset({"0xabcdef0123456789"}))
    assert al.is_allowed("0xabcdef0123456789") is True


def test_is_allowed_case_insensitive_upper() -> None:
    """Address comparison must be case-insensitive (upper-case input)."""
    al = WithdrawalAllowlist(frozenset({"0xabcdef0123456789"}))
    assert al.is_allowed("0xABCDEF0123456789") is True


def test_is_allowed_case_insensitive_mixed() -> None:
    """Mixed-case address must match a lower-cased entry."""
    al = WithdrawalAllowlist(frozenset({"0xabcdef0123456789"}))
    assert al.is_allowed("0xAbCdEf0123456789") is True


def test_is_not_allowed_absent_address() -> None:
    """An address not in the list must be refused."""
    al = WithdrawalAllowlist(frozenset({"0xabcdef0123456789"}))
    assert al.is_allowed("0x000000000000") is False


def test_empty_allowlist_refuses_all() -> None:
    """An empty allowlist must refuse every address (fail-closed)."""
    al = WithdrawalAllowlist(frozenset())
    assert al.is_allowed("0xabcdef0123456789") is False
    assert al.is_allowed("0x0000000000000000") is False


def test_len_returns_count() -> None:
    """__len__ returns the number of permitted addresses."""
    al = WithdrawalAllowlist(frozenset({"0xaaa", "0xbbb", "0xccc"}))
    assert len(al) == 3


# ---------------------------------------------------------------------------
# Load from file
# ---------------------------------------------------------------------------


def test_load_from_yaml_file() -> None:
    """WithdrawalAllowlist.load() reads addresses from a YAML file."""
    yaml_content = "addresses:\n  - '0xabcdef'\n  - '0x123456'\n"
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(yaml_content)
        path = Path(f.name)

    try:
        al = WithdrawalAllowlist.load(path)
        assert len(al) == 2
        assert al.is_allowed("0xABCDEF") is True
        assert al.is_allowed("0x123456") is True
        assert al.is_allowed("0x000000") is False
    finally:
        path.unlink()


def test_load_missing_file_returns_empty() -> None:
    """Missing file returns an empty allowlist (fail-closed)."""
    al = WithdrawalAllowlist.load(Path("/nonexistent/path/that/does/not/exist.yaml"))
    assert len(al) == 0
    assert al.is_allowed("0xabcdef") is False


def test_load_invalid_yaml_returns_empty() -> None:
    """A file with invalid YAML returns an empty allowlist."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(": invalid: yaml: [[[")
        path = Path(f.name)

    try:
        al = WithdrawalAllowlist.load(path)
        assert len(al) == 0
    finally:
        path.unlink()


def test_load_non_dict_yaml_returns_empty() -> None:
    """A YAML file that is not a mapping returns an empty allowlist."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write("- just a list\n")
        path = Path(f.name)

    try:
        al = WithdrawalAllowlist.load(path)
        assert len(al) == 0
    finally:
        path.unlink()


def test_load_empty_addresses_list() -> None:
    """A YAML file with an empty addresses list returns an empty allowlist."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write("addresses: []\n")
        path = Path(f.name)

    try:
        al = WithdrawalAllowlist.load(path)
        assert len(al) == 0
    finally:
        path.unlink()
