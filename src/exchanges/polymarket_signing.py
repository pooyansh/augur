"""Polymarket EIP-712 signing isolation module — CLOB V2 protocol.

This module is the ONLY place where the L1 private key is touched.
It is loaded once at startup via the secrets loader and NEVER accepted as a
function argument from outside this module.

Two signing paths are supported:

EOA (signatureType=0): standard EIP-712 + eth_account, maker == signer == EOA address.

POLY_1271 (signatureType=3): used with deposit wallets (new Polymarket accounts).
  - maker == signer == deposit_wallet_address (ERC-1967 proxy contract)
  - The EOA private key signs via the Solady ERC-7739 TypedDataSign scheme
  - The resulting signature encodes: inner_sig || app_domain_sep || contents_hash || type_string
  - The CTF Exchange V2 contract calls isValidSignature() on the deposit wallet

CTF Exchange V2 domain (Polygon mainnet):
    name: "Polymarket CTF Exchange"
    version: "2"
    chainId: 137
    verifyingContract: "0xE111180000d2663C0091e4f400237545B87B996B"

Order struct (11 fields — V2):
    salt, maker, signer, tokenId, makerAmount, takerAmount,
    side, signatureType, timestamp, metadata, builder

client_order_id → salt mapping (deterministic, survives restarts):
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

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak as eth_keccak  # type: ignore[import-untyped]
from eth_utils import to_checksum_address  # type: ignore[import-untyped]

from src.exchanges.base import OrderIntent, Side

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EIP-712 constants — CTF Exchange V2
# ---------------------------------------------------------------------------

_CHAIN_ID = 137  # Polygon mainnet
_CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"

_DOMAIN: dict[str, Any] = {
    "name": "Polymarket CTF Exchange",
    "version": "2",
    "chainId": _CHAIN_ID,
    "verifyingContract": _CTF_EXCHANGE_V2,
}

_ORDER_STRUCT: list[dict[str, str]] = [
    {"name": "salt", "type": "uint256"},
    {"name": "maker", "type": "address"},
    {"name": "signer", "type": "address"},
    {"name": "tokenId", "type": "uint256"},
    {"name": "makerAmount", "type": "uint256"},
    {"name": "takerAmount", "type": "uint256"},
    {"name": "side", "type": "uint8"},
    {"name": "signatureType", "type": "uint8"},
    {"name": "timestamp", "type": "uint256"},
    {"name": "metadata", "type": "bytes32"},
    {"name": "builder", "type": "bytes32"},
]

_EIP712_DOMAIN_STRUCT: list[dict[str, str]] = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

# 32 zero bytes
_BYTES32_ZERO = bytes(32)

# Side encoding: 0=BUY, 1=SELL
_SIDE_ENCODING = {Side.BUY: 0, Side.SELL: 1}

# ---------------------------------------------------------------------------
# POLY_1271 (Solady ERC-7739) constants
# ---------------------------------------------------------------------------

_ORDER_TYPE_STRING = (
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
)
_SOLADY_TYPE_STRING = (
    "TypedDataSign(Order contents,string name,string version,uint256 chainId,"
    "address verifyingContract,bytes32 salt)" + _ORDER_TYPE_STRING
)
_DOMAIN_TYPE_STRING = (
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)

_ORDER_TYPE_HASH = eth_keccak(text=_ORDER_TYPE_STRING)
_DOMAIN_TYPE_HASH = eth_keccak(text=_DOMAIN_TYPE_STRING)
_SOLADY_TYPE_HASH = eth_keccak(text=_SOLADY_TYPE_STRING)
_DEPOSIT_WALLET_NAME_HASH = eth_keccak(text="DepositWallet")
_DEPOSIT_WALLET_VERSION_HASH = eth_keccak(text="1")
_DEPOSIT_WALLET_DOMAIN_SALT = bytes(32)


def _app_domain_separator() -> bytes:
    """Pre-compute the CTF Exchange V2 EIP-712 domain separator bytes."""
    return eth_keccak(
        primitive=abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                _DOMAIN_TYPE_HASH,
                eth_keccak(text="Polymarket CTF Exchange"),
                eth_keccak(text="2"),
                _CHAIN_ID,
                _CTF_EXCHANGE_V2,
            ],
        )
    )


_APP_DOMAIN_SEPARATOR = _app_domain_separator()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignedOrder:
    """EIP-712 V2 signed order ready for submission to the CLOB."""

    salt: int
    maker: str
    signer: str
    token_id: str
    maker_amount: int
    taker_amount: int
    expiration: int
    side: int
    signature_type: int
    timestamp: int
    metadata: bytes
    builder: bytes
    signature: str

    def to_wire(self) -> dict[str, Any]:
        """Serialize to CLOB POST /order wire format (V2).

        Field encoding (verified against py-clob-client-v2):
        - salt: integer (JSON number).
        - side: "BUY" or "SELL" string.
        - signatureType: integer.
        - tokenId, makerAmount, takerAmount, expiration, timestamp: decimal strings.
        - metadata, builder: 0x-prefixed 64-hex-char strings (bytes32).
        """
        side_str = "BUY" if self.side == 0 else "SELL"
        return {
            "salt": self.salt,
            "maker": self.maker,
            "signer": self.signer,
            "tokenId": self.token_id,
            "makerAmount": str(self.maker_amount),
            "takerAmount": str(self.taker_amount),
            "side": side_str,
            "expiration": str(self.expiration),
            "signatureType": self.signature_type,
            "timestamp": str(self.timestamp),
            "metadata": "0x" + self.metadata.hex(),
            "builder": "0x" + self.builder.hex(),
            "signature": self.signature,
        }


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def derive_salt(client_order_id: str) -> int:
    """Derive a deterministic EIP-712 salt from a client_order_id.

    Takes the first 10 hex characters (5 bytes) and interprets them as a
    big-endian integer. Using 5 bytes caps the salt at 2^40 (~1.1e12),
    which is safely within JavaScript's MAX_SAFE_INTEGER (2^53 - 1).
    The Polymarket CLOB parses salt as a JSON number; salts exceeding 2^53
    lose precision in JavaScript and cause signature verification failures.

    Args:
        client_order_id: Hex string produced by BaseBot (blake2s, 16 hex chars).

    Returns:
        Non-negative integer suitable for the EIP-712 ``salt`` field.
    """
    return int.from_bytes(bytes.fromhex(client_order_id[:10]), "big")


# ---------------------------------------------------------------------------
# Signing module
# ---------------------------------------------------------------------------


class SigningModule:
    """EIP-712 V2 signing and L2 HMAC auth for Polymarket.

    Supports two signing modes:
    - EOA (signature_type=0): standard EIP-712 signing.
    - POLY_1271 (signature_type=3): deposit wallet flow, Solady ERC-7739 scheme.

    The L1 private key is loaded once at construction and held in memory.
    It is NEVER logged, NEVER returned from any method, and NEVER accepted
    as a parameter from external callers.

    Args:
        l1_private_key: Raw hex private key string (from secrets). Never log.
        l2_api_key: L2 API key for HMAC headers.
        l2_secret: L2 HMAC secret (base64-encoded).
        l2_passphrase: L2 passphrase.
        wallet_address: EOA address (raw, lowercase from secrets).
        signature_type: 0=EOA, 3=POLY_1271.
        deposit_wallet_address: Deposit wallet contract address. Required
            when signature_type=3; unused otherwise.
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
        deposit_wallet_address: str | None = None,
    ) -> None:
        self._account = Account.from_key(l1_private_key)
        self._l2_api_key = l2_api_key
        self._l2_secret = l2_secret
        self._l2_passphrase = l2_passphrase
        self._signature_type = signature_type

        # For POLY_ADDRESS header, use the checksummed EOA from the Account object
        self._eoa_address: str = self._account.address  # checksummed

        if signature_type == 3:  # POLY_1271
            if not deposit_wallet_address:
                raise ValueError(
                    "deposit_wallet_address is required for POLY_1271 (signature_type=3)"
                )
            dw = to_checksum_address(deposit_wallet_address)
            self._maker_address: str = dw  # deposit wallet (raw lowercase)
            self._signer_address: str = dw  # same for POLY_1271
        else:  # EOA (0), POLY_PROXY (1), GNOSIS_SAFE (2)
            # maker uses raw address from config (lowercase) — matches py-clob-client-v2
            self._maker_address = wallet_address
            self._signer_address = self._eoa_address  # checksummed

        logger.debug(
            "SigningModule initialised (V2)",
            extra={
                "eoa": self._eoa_address,
                "maker": self._maker_address,
                "sig_type": signature_type,
            },
        )

    def sign_order(self, intent: OrderIntent) -> SignedOrder:
        """Build and sign a V2 EIP-712 order from an OrderIntent.

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
        timestamp_ms = int(time.time() * 1000)

        scale = Decimal("1000000")
        if intent.side == Side.BUY:
            maker_amount = int((intent.price * intent.size * scale).to_integral_value())
            taker_amount = int((intent.size * scale).to_integral_value())
        else:
            maker_amount = int((intent.size * scale).to_integral_value())
            taker_amount = int((intent.price * intent.size * scale).to_integral_value())

        eip712_message = {
            "salt": salt,
            "maker": self._maker_address,
            "signer": self._signer_address,
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "side": side_int,
            "signatureType": self._signature_type,
            "timestamp": timestamp_ms,
            "metadata": _BYTES32_ZERO,
            "builder": _BYTES32_ZERO,
        }

        if self._signature_type == 3:  # POLY_1271
            signature_hex = self._sign_poly_1271(eip712_message)
        else:  # EOA standard EIP-712
            typed_data = {
                "types": {
                    "EIP712Domain": _EIP712_DOMAIN_STRUCT,
                    "Order": _ORDER_STRUCT,
                },
                "domain": _DOMAIN,
                "primaryType": "Order",
                "message": eip712_message,
            }
            signable = encode_typed_data(full_message=typed_data)
            signed = self._account.sign_message(signable)
            raw = signed.signature.hex()
            signature_hex = raw if raw.startswith("0x") else "0x" + raw

        return SignedOrder(
            salt=salt,
            maker=self._maker_address,
            signer=self._signer_address,
            token_id=token_id,
            maker_amount=maker_amount,
            taker_amount=taker_amount,
            expiration=0,
            side=side_int,
            signature_type=self._signature_type,
            timestamp=timestamp_ms,
            metadata=_BYTES32_ZERO,
            builder=_BYTES32_ZERO,
            signature=signature_hex,
        )

    def _sign_poly_1271(self, message: dict[str, Any]) -> str:
        """Solady ERC-7739 TypedDataSign signature for POLY_1271 deposit wallets.

        The deposit wallet contract verifies this via EIP-1271 isValidSignature().
        The signature format:
            0x || ecdsa_sig(65) || app_domain_sep(32) || contents_hash(32)
               || type_string_hex || type_string_len_be(2)

        Args:
            message: The EIP-712 order message dict (Python-typed fields).

        Returns:
            Hex-encoded signature string starting with 0x.
        """
        # 1. Hash the order contents
        contents_hash = eth_keccak(
            primitive=abi_encode(
                [
                    "bytes32",  # typeHash
                    "uint256",  # salt
                    "address",  # maker
                    "address",  # signer
                    "uint256",  # tokenId
                    "uint256",  # makerAmount
                    "uint256",  # takerAmount
                    "uint8",  # side
                    "uint8",  # signatureType
                    "uint256",  # timestamp
                    "bytes32",  # metadata
                    "bytes32",  # builder
                ],
                [
                    _ORDER_TYPE_HASH,
                    int(message["salt"]),
                    message["maker"],
                    message["signer"],
                    int(message["tokenId"]),
                    int(message["makerAmount"]),
                    int(message["takerAmount"]),
                    int(message["side"]),
                    int(message["signatureType"]),
                    int(message["timestamp"]),
                    bytes(message["metadata"])
                    if isinstance(message["metadata"], (bytes, bytearray))
                    else bytes(32),
                    bytes(message["builder"])
                    if isinstance(message["builder"], (bytes, bytearray))
                    else bytes(32),
                ],
            )
        )

        # 2. Build the TypedDataSign struct hash
        typed_data_sign_hash = eth_keccak(
            primitive=abi_encode(
                [
                    "bytes32",  # SOLADY_TYPE_HASH
                    "bytes32",  # contents_hash
                    "bytes32",  # deposit wallet name hash
                    "bytes32",  # deposit wallet version hash
                    "uint256",  # chainId
                    "address",  # verifyingContract (deposit wallet = signer)
                    "bytes32",  # domain salt (zero)
                ],
                [
                    _SOLADY_TYPE_HASH,
                    contents_hash,
                    _DEPOSIT_WALLET_NAME_HASH,
                    _DEPOSIT_WALLET_VERSION_HASH,
                    _CHAIN_ID,
                    message["signer"],  # deposit wallet address
                    _DEPOSIT_WALLET_DOMAIN_SALT,
                ],
            )
        )

        # 3. Final digest: EIP-191 prefix + app domain sep + struct hash
        digest = eth_keccak(primitive=b"\x19\x01" + _APP_DOMAIN_SEPARATOR + typed_data_sign_hash)

        # 4. Sign the digest with EOA private key
        signed = Account._sign_hash(digest, private_key=self._account.key)
        inner_sig = signed.signature.hex()
        if inner_sig.startswith("0x"):
            inner_sig = inner_sig[2:]

        # 5. Encode type string suffix (Solady format)
        type_bytes = _ORDER_TYPE_STRING.encode("utf-8")
        type_hex = type_bytes.hex()
        type_len = len(type_bytes).to_bytes(2, "big").hex()

        return (
            "0x"
            + inner_sig
            + _APP_DOMAIN_SEPARATOR.hex()
            + contents_hash.hex()
            + type_hex
            + type_len
        )

    def build_l2_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """Build Polymarket L2 HMAC authentication headers.

        POLY_ADDRESS uses the EOA address (not the deposit wallet).

        Args:
            method: HTTP method (e.g. "GET", "POST").
            path: Request path including query string (e.g. "/order").
            body: Request body string (empty for GET requests).

        Returns:
            Dict of five POLY_* headers required for authenticated endpoints.
        """
        import base64

        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + body
        raw_sig = hmac.new(
            key=base64.urlsafe_b64decode(self._l2_secret),
            msg=message.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sig_b64 = base64.urlsafe_b64encode(raw_sig).decode("utf-8")

        return {
            "POLY_ADDRESS": self._eoa_address,
            "POLY_SIGNATURE": sig_b64,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self._l2_api_key,
            "POLY_PASSPHRASE": self._l2_passphrase,
        }
