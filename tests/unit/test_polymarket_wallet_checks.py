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

    assert any(
        "balance_high" in r.message or "balance_ceiling" in r.message or "high" in r.message
        for r in caplog.records
    ), "Expected a warning about high balance"


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


# ---------------------------------------------------------------------------
# _rpc_eth_call retry behavior (mocks the HTTP client, not _rpc_eth_call
# itself, since that's the seam under test here).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_eth_call_retries_transient_failure_then_succeeds() -> None:
    cfg = _make_config()
    adapter = PolymarketAdapter(Mode.PAPER, cfg)
    adapter._http = AsyncMock(spec=httpx.AsyncClient)

    success_resp = httpx.Response(
        status_code=200,
        json={"result": _uint256_hex(0)},
        request=httpx.Request("POST", cfg.polygon_rpc_url),
    )

    call_count = 0

    async def _fake_post(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.NetworkError("connection reset")
        return success_resp

    adapter._http.post = AsyncMock(side_effect=_fake_post)

    result = await adapter._rpc_eth_call(cfg.polygon_rpc_url, _DUMMY_ADDR, "0xdd62ed3e")

    assert result == _uint256_hex(0)
    assert call_count == 3  # two failures + one success, within the retry budget


@pytest.mark.asyncio
async def test_rpc_eth_call_does_not_retry_on_401() -> None:
    cfg = _make_config()
    adapter = PolymarketAdapter(Mode.PAPER, cfg)
    adapter._http = AsyncMock(spec=httpx.AsyncClient)

    unauthorized_resp = httpx.Response(
        status_code=401,
        text="Unauthorized",
        request=httpx.Request("POST", cfg.polygon_rpc_url),
    )

    call_count = 0

    async def _fake_post(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return unauthorized_resp

    adapter._http.post = AsyncMock(side_effect=_fake_post)

    with pytest.raises(httpx.HTTPStatusError):
        await adapter._rpc_eth_call(cfg.polygon_rpc_url, _DUMMY_ADDR, "0xdd62ed3e")

    assert call_count == 1  # no retry on 401 — fails fast


@pytest.mark.asyncio
async def test_rpc_eth_call_401_still_fails_open_via_startup_wallet_checks() -> None:
    """A 401 propagates out of _rpc_eth_call (no retry), but the outer
    _startup_wallet_checks try/except still catches it and does not raise —
    the fail-open contract is unchanged.
    """
    cfg = _make_config()
    adapter = PolymarketAdapter(Mode.PAPER, cfg)
    adapter._http = AsyncMock(spec=httpx.AsyncClient)

    unauthorized_resp = httpx.Response(
        status_code=401,
        text="Unauthorized",
        request=httpx.Request("POST", cfg.polygon_rpc_url),
    )
    adapter._http.post = AsyncMock(return_value=unauthorized_resp)

    await adapter._startup_wallet_checks()  # best-effort: must not raise
