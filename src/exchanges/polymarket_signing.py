"""Polymarket EIP-712 signing isolation module.

This module is the ONLY place where the L1 private key is touched.
It is loaded once at startup via the secrets loader and NEVER accepted as a
function argument from outside this module.

EIP-712 domain (Polygon mainnet):
    name: "Polymarket CTF Exchange"
    version: "1"
    chainId: 137
    verifyingContract: "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    (TODO: verify this address against the live CTF Exchange contract before
     enabling live trading)

Order struct (12 fields):
    salt, maker, signer, taker, tokenId, makerAmount, takerAmount,
    expiration, nonce, feeRateBps, side, signatureType

client_order_id → salt mapping (Option B — deterministic, no caching needed):
    salt = int.from_bytes(bytes.fromhex(client_order_id[:16]), 'big')
"""

from __future__ import annotations

__all__ = [
    "SignedOrder",
    "SigningModule",
    "derive_salt",
]

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data

from src.exchanges.base import OrderIntent, Side

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EIP-712 constants
# ---------------------------------------------------------------------------

_DOMAIN: dict[str, Any] = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": 137,  # Polygon mainnet
    # TODO(phase-4): verify this address against the live CTF Exchange contract
    # before enabling live trading.  Source: py-clob-client open-source code.
    "verifyingContract": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
}

_ORDER_TYPES: dict[str, list[dict[str, str]]] = {
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ]
}

# Standard zero address for taker (open orders)
_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# USDC has 6 decimal places
_USDC_DECIMALS = 6

# Side encoding: 0=BUY, 1=SELL
_SIDE_ENCODING = {Side.BUY: 0, Side.SELL: 1}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignedOrder:
    """EIP-712 signed order ready for submission to the CLOB.

    Args:
        salt: Deterministic nonce derived from client_order_id.
        maker: Wallet address (checksummed).
        signer: Signer address (same as maker for EOA).
        taker: Zero address for open orders.
        token_id: ERC-1155 outcome token ID (decimal string).
        maker_amount: USDC amount in micro-USDC (6 decimals).
        taker_amount: Share amount in micro-shares (6 decimals).
        expiration: 0 for GTC orders.
        nonce: 0 for standard orders.
        fee_rate_bps: Fee rate in basis points.
        side: 0=BUY, 1=SELL.
        signature_type: 0=EOA, 1=proxy, 2=gnosis.
        signature: Hex-encoded EIP-712 signature.
    """

    salt: int
    maker: str
    signer: str
    taker: str
    token_id: str
    maker_amount: int
    taker_amount: int
    expiration: int
    nonce: int
    fee_rate_bps: int
    side: int
    signature_type: int
    signature: str

    def to_wire(self) -> dict[str, Any]:
        """Serialize to the CLOB POST /order wire format.

        Returns:
            Dict matching Polymarket CLOB order schema.
        """
        return {
            "salt": self.salt,
            "maker": self.maker,
            "signer": self.signer,
            "taker": self.taker,
            "tokenId": self.token_id,
            "makerAmount": str(self.maker_amount),
            "takerAmount": str(self.taker_amount),
            "expiration": str(self.expiration),
            "nonce": str(self.nonce),
            "feeRateBps": str(self.fee_rate_bps),
            "side": str(self.side),
            "signatureType": str(self.signature_type),
            "signature": self.signature,
        }


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def derive_salt(client_order_id: str) -> int:
    """Derive a deterministic EIP-712 salt from a client_order_id.

    Option B: take the first 16 hex characters (8 bytes) of the
    client_order_id and interpret them as a big-endian integer.

    This is deterministic, requires no caching, and survives restarts because
    the client_order_id itself is stable across retries of the same intent.

    Args:
        client_order_id: Hex string produced by BaseBot (blake2s, 16 hex chars).

    Returns:
        Non-negative integer suitable for the EIP-712 ``salt`` field.
    """
    return int.from_bytes(bytes.fromhex(client_order_id[:16]), "big")


# ---------------------------------------------------------------------------
# Signing module
# ---------------------------------------------------------------------------


class SigningModule:
    """EIP-712 signing and L2 HMAC auth for Polymarket.

    The L1 private key is loaded once at construction and held in memory.
    It is NEVER logged, NEVER returned from any method, and NEVER accepted
    as a parameter from external callers.

    Args:
        l1_private_key: Raw hex private key string (loaded from secrets).
            Never log this value.
        l2_api_key: L2 API key for HMAC headers.
        l2_secret: L2 HMAC secret.
        l2_passphrase: L2 passphrase.
        wallet_address: Checksummed Polygon wallet address.
        signature_type: 0=EOA, 1=proxy, 2=gnosis safe.
        fee_rate_bps: Fee rate in basis points (default 0 — set by CLOB).
    """

    def __init__(
        self,
        *,
        l1_private_key: str,
        l2_api_key: str,
        l2_secret: str,
        l2_passphrase: str,
        wallet_address: str,
        signature_type: int,
        fee_rate_bps: int = 0,
    ) -> None:
        # Load L1 key — never log, never re-expose
        self._account = Account.from_key(l1_private_key)
        self._l2_api_key = l2_api_key
        self._l2_secret = l2_secret
        self._l2_passphrase = l2_passphrase
        self._wallet_address = wallet_address
        self._signature_type = signature_type
        self._fee_rate_bps = fee_rate_bps

        logger.debug(
            "SigningModule initialised",
            extra={"wallet": wallet_address, "sig_type": signature_type},
        )

    def sign_order(self, intent: OrderIntent) -> SignedOrder:
        """Build and sign an EIP-712 order from an OrderIntent.

        Amount encoding:
            BUY:  makerAmount = round(price * size * 1_000_000) USDC micro-units
                  takerAmount = round(size * 1_000_000) share micro-units
            SELL: makerAmount = round(size * 1_000_000) share micro-units
                  takerAmount = round(price * size * 1_000_000) USDC micro-units

        Args:
            intent: Order intent from BaseBot.

        Returns:
            Fully-signed :class:`SignedOrder` ready for the CLOB.
        """
        salt = derive_salt(intent.client_order_id)
        token_id = intent.market.token_id
        side_int = _SIDE_ENCODING[intent.side]

        scale = Decimal("1000000")  # 6 decimal places
        if intent.side == Side.BUY:
            maker_amount = int((intent.price * intent.size * scale).to_integral_value())
            taker_amount = int((intent.size * scale).to_integral_value())
        else:  # SELL
            maker_amount = int((intent.size * scale).to_integral_value())
            taker_amount = int((intent.price * intent.size * scale).to_integral_value())

        order_data = {
            "salt": salt,
            "maker": self._wallet_address,
            "signer": self._wallet_address,
            "taker": _ZERO_ADDRESS,
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": 0,
            "nonce": 0,
            "feeRateBps": self._fee_rate_bps,
            "side": side_int,
            "signatureType": self._signature_type,
        }

        structured_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                **_ORDER_TYPES,
            },
            "domain": _DOMAIN,
            "primaryType": "Order",
            "message": order_data,
        }

        signable = encode_typed_data(full_message=structured_data)
        signed = self._account.sign_message(signable)
        signature_hex: str = signed.signature.hex()

        return SignedOrder(
            salt=salt,
            maker=self._wallet_address,
            signer=self._wallet_address,
            taker=_ZERO_ADDRESS,
            token_id=token_id,
            maker_amount=maker_amount,
            taker_amount=taker_amount,
            expiration=0,
            nonce=0,
            fee_rate_bps=self._fee_rate_bps,
            side=side_int,
            signature_type=self._signature_type,
            signature=signature_hex,
        )

    def build_l2_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """Build Polymarket L2 HMAC authentication headers.

        The signature covers: timestamp + method.upper() + path + body
        using HMAC-SHA256 with the L2 secret, base64-encoded.

        Args:
            method: HTTP method (e.g. "GET", "POST").
            path: Request path including query string (e.g. "/order").
            body: Request body string (empty string for GET requests).

        Returns:
            Dict of headers required for authenticated CLOB endpoints.
        """
        import base64

        timestamp = str(int(time.time() * 1000))
        message = timestamp + method.upper() + path + body
        raw_signature = hmac.new(
            key=self._l2_secret.encode("utf-8"),
            msg=message.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sig_b64 = base64.b64encode(raw_signature).decode("utf-8")

        return {
            "POLY_ADDRESS": self._wallet_address,
            "POLY_SIGNATURE": sig_b64,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self._l2_api_key,
            "POLY_PASSPHRASE": self._l2_passphrase,
        }
