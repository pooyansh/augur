"""Unit tests for Phase-4 wallet safety checks in PolymarketAdapter.

All tests mock _rpc_eth_call so no live network calls are made.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.exchanges.polymarket import PolymarketAdapter, PolymarketConfig
from src.exchanges.base import Mode

_DUMMY_KEY = "0x" + "a" * 64
_DUMMY_ADDR = "0x" + "a" * 40


def _make_config(**overrides: object) -> PolymarketConfig:
    base = {
        "l1_private_key": _DUMMY_KEY,
        "l2_api_key": "key",
        "l2_secret": "secret",
        "l2_passphrase": "pass",
        "wallet_address": _DUMMY_ADDR,
    }
    base.update(overrides)
    return PolymarketConfig(**base)  # type: ignore[arg-type]


def _uint256_hex(value: int) -> str:
    """Encode a uint256 as a 0x-prefixed 64-char hex string."""
    return "0x" + hex(value)[2:].zfill(64)


@pytest.mark.asyncio
async def test_sig_type_3_without_deposit_wallet_raises() -> None:
    cfg = _make_config(signature_type=3, deposit_wallet_address=None)
    adapter = PolymarketAdapter(Mode.PAPER, cfg)
    with pytest.raises(RuntimeError, match="deposit_wallet_address"):
        await adapter._startup_wallet_checks()


@pytest.mark.asyncio
async def test_sig_type_0_passes() -> None:
    cfg = _make_config(signature_type=0)
    adapter = PolymarketAdapter(Mode.PAPER, cfg)
    # Both RPC calls return 0 (no allowance, no balance)
    with patch.object(adapter, "_rpc_eth_call", new=AsyncMock(return_value=_uint256_hex(0))):
        await adapter._startup_wallet_checks()  # must not raise


@pytest.mark.asyncio
async def test_allowance_under_cap_passes() -> None:
    cfg = _make_config(max_allowance_usdc=Decimal("100000"))
    adapter = PolymarketAdapter(Mode.PAPER, cfg)
    # 1 000 USDC allowance (6 decimals → 1_000_000_000 raw)
    allowance_raw = _uint256_hex(1_000 * 1_000_000)
    with patch.object(adapter, "_rpc_eth_call", new=AsyncMock(return_value=allowance_raw)):
        await adapter._startup_wallet_checks()  # must not raise


@pytest.mark.asyncio
async def test_allowance_over_cap_raises() -> None:
    cfg = _make_config(max_allowance_usdc=Decimal("100000"))
    adapter = PolymarketAdapter(Mode.PAPER, cfg)
    # 200 000 USDC allowance > 100 000 cap
    allowance_raw = _uint256_hex(200_000 * 1_000_000)
    balance_raw = _uint256_hex(0)

    async def _fake_eth_call(rpc_url: str, to: str, data: str) -> str:
        # allowance call uses selector 0xdd62ed3e; balance uses 0x70a08231
        if data.startswith("0xdd62ed3e"):
            return allowance_raw
        return balance_raw

    with patch.object(adapter, "_rpc_eth_call", new=AsyncMock(side_effect=_fake_eth_call)):
        with pytest.raises(RuntimeError, match="allowance"):
            await adapter._startup_wallet_checks()


@pytest.mark.asyncio
async def test_balance_over_ceiling_warns_not_raises(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _make_config(balance_ceiling_usdc=Decimal("100000"))
    adapter = PolymarketAdapter(Mode.PAPER, cfg)
    # allowance under cap, balance over ceiling
    allowance_raw = _uint256_hex(0)
    balance_raw = _uint256_hex(200_000 * 1_000_000)

    async def _fake_eth_call(rpc_url: str, to: str, data: str) -> str:
        if data.startswith("0x70a08231"):
            return balance_raw
        return allowance_raw

    import logging

    with caplog.at_level(logging.WARNING):
        with patch.object(adapter, "_rpc_eth_call", new=AsyncMock(side_effect=_fake_eth_call)):
            await adapter._startup_wallet_checks()  # must NOT raise

    assert any("balance_high" in r.message or "balance_ceiling" in r.message or "high" in r.message
               for r in caplog.records), "Expected a warning about high balance"


@pytest.mark.asyncio
async def test_rpc_failure_does_not_block_startup() -> None:
    cfg = _make_config()
    adapter = PolymarketAdapter(Mode.PAPER, cfg)
    # Simulate a network error on every RPC call
    with patch.object(
        adapter,
        "_rpc_eth_call",
        new=AsyncMock(side_effect=httpx.NetworkError("connection refused")),
    ):
        await adapter._startup_wallet_checks()  # best-effort: must not raise
