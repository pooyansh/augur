"""Polymarket CLOB exchange adapter — Phase 4.

Implements :class:`ExchangeAdapter` for Polymarket's Central Limit Order Book.
In paper mode, routes to :class:`~src.exchanges.polymarket_paper.PolymarketPaperSimulator`.
In live mode, transmits EIP-712-signed orders to the real CLOB.

Two authentication tiers:
  - L1: EIP-712 typed-data signing for order payloads (via SigningModule).
  - L2: HMAC-SHA256 headers for authenticated REST endpoints.

Identifier model:
  - market_id == condition_id (hex string, identifies the market).
  - token_id  == ERC-1155 outcome token id (identifies YES or NO side).

Error classification:
  ┌──────────────────┬──────────────────────────────────────────────┐
  │ Category         │ Trigger                                      │
  ├──────────────────┼──────────────────────────────────────────────┤
  │ retryable        │ 429, 503, 504, any network/timeout error     │
  │ fatal            │ 4xx (not 401/403/404), bad price/size        │
  │ auth             │ 401, 403                                     │
  │ not_found        │ 404                                          │
  └──────────────────┴──────────────────────────────────────────────┘
"""

from __future__ import annotations

__all__ = [
    "PolymarketAdapter",
    "PolymarketConfig",
]

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.exchanges.base import (
    CancelEvent,
    ExchangeAdapter,
    ExchangeEvent,
    FillEvent,
    Market,
    Mode,
    OrderIntent,
    OrderResult,
    RejectionEvent,
    SettlementEvent,
    Side,
)
from src.exchanges.polymarket_signing import SigningModule

logger = logging.getLogger(__name__)

# Polygon mainnet contract addresses (public, not secrets)
_USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon
_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Polymarket CTF Exchange

# Default public RPC — known to rate-limit/reject unauthenticated bot traffic.
# See .claude/rules/05-exchanges.md § Wallet safety checks for the documented gap.
_DEFAULT_PUBLIC_RPC_URL = "https://polygon-rpc.com"

# Retry policy for _rpc_eth_call: a small number of short-backoff attempts so
# a real authenticated provider's occasional transient hiccup doesn't
# immediately trip the (intentional) fail-open path in _startup_wallet_checks.
_RPC_RETRY_ATTEMPTS = 3
_RPC_RETRY_WAIT_INITIAL_SECONDS = 0.5
_RPC_RETRY_WAIT_MAX_SECONDS = 4.0

# HTTP status codes worth retrying on an RPC call — transient server/rate-limit
# conditions.  401/403 are deliberately excluded: they mean the RPC URL/auth is
# wrong, not that the request was transient, so retrying wastes time before the
# fail-open path (correctly) kicks in.
_RPC_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def _is_retryable_rpc_exception(exc: BaseException) -> bool:
    """Return whether an exception from an RPC call is worth retrying.

    Args:
        exc: Exception raised by the RPC HTTP call.

    Returns:
        ``True`` for network errors, timeouts, and retryable HTTP status
        codes (429/5xx).  ``False`` for everything else, notably 401/403
        auth failures.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RPC_RETRYABLE_STATUS_CODES
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


class PolymarketConfig(BaseModel):
    """Configuration for the Polymarket adapter.

    All secret fields are loaded via the secrets loader — never via env vars
    or CLI arguments.

    Args:
        l1_private_key: Raw hex private key for EIP-712 order signing.
            Loaded from secrets; never logged.
        l2_api_key: L2 HMAC API key.
        l2_secret: L2 HMAC secret.
        l2_passphrase: L2 passphrase.
        wallet_address: Checksummed Polygon wallet address.
        signature_type: 0=EOA, 1=proxy wallet, 2=Gnosis Safe.
        max_allowance_usdc: Maximum on-chain USDC allowance; refuse startup
            if live allowance exceeds this.
        balance_ceiling_usdc: Alert at warn if hot-wallet USDC exceeds this.
        allowance_cap_usdc: Maximum allowance to approve when setting up.
        clob_host: Base URL of the Polymarket CLOB REST API.
        gamma_host: Base URL of the Gamma metadata API.
        ws_host: WebSocket URL for market event subscriptions.
    """

    l1_private_key: str
    l2_api_key: str
    l2_secret: str
    l2_passphrase: str
    wallet_address: str
    signature_type: int = 0  # 0=EOA, 1=proxy, 2=gnosis, 3=POLY_1271
    # Deposit wallet address — required when signature_type=3 (POLY_1271 flow).
    # Derived deterministically from wallet_address via the deposit wallet factory.
    deposit_wallet_address: str | None = None
    max_allowance_usdc: Decimal = Decimal("100000")
    balance_ceiling_usdc: Decimal = Decimal("100000")
    allowance_cap_usdc: Decimal = Decimal("100000")
    polygon_rpc_url: str = "https://polygon-rpc.com"
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    ws_host: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------


class PolymarketAdapter(ExchangeAdapter):
    """Polymarket CLOB exchange adapter.

    Paper mode: routes all placement to an in-process simulator backed by
    live Polymarket WebSocket data.  Real money never moves.

    Live mode: transmits EIP-712-signed orders to the CLOB REST API.

    Args:
        mode: :class:`~src.exchanges.base.Mode.PAPER` or
            :class:`~src.exchanges.base.Mode.LIVE`.
        config: :class:`PolymarketConfig` loaded from secrets.
    """

    venue: ClassVar[str] = "polymarket"

    # Market cache TTL (seconds)
    _MARKET_CACHE_TTL = 60

    def __init__(self, mode: Mode, config: PolymarketConfig) -> None:
        super().__init__(mode)
        self._config = config
        self._http: httpx.AsyncClient | None = None
        self._signing: SigningModule | None = None

        # Market info cache: market_id -> (Market, fetched_at)
        self._market_cache: dict[str, tuple[Market, datetime]] = {}

        # In-flight order tracking: client_order_id -> exchange_order_id
        self._inflight: dict[str, str] = {}

        # Reconciliation state
        self._reconcile_failed: bool = False

        # Paper simulator (lazily initialised)
        self._paper: PolymarketPaperSimulator | None = None

        # Subscribed token IDs for events
        self._subscribed_token_ids: list[str] = []

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> PolymarketAdapter:
        """Start the HTTP client, initialise signing, run startup checks."""
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"Content-Type": "application/json"},
        )

        if self._mode == Mode.LIVE:
            if self._config.polygon_rpc_url == _DEFAULT_PUBLIC_RPC_URL:
                logger.warning(
                    "polymarket_default_public_rpc_in_live_mode",
                    extra={
                        "message": (
                            "wallet safety checks are using the default public Polygon "
                            "RPC (polygon-rpc.com), which is known to rate-limit/reject "
                            "bot traffic — set polymarket.polygon_rpc_url in "
                            "secrets/exchanges.enc.yaml to an authenticated provider "
                            "(Alchemy/Infura/Ankr all have free tiers) for reliable "
                            "pre-trade safety checks."
                        ),
                    },
                )

            # Auto-detect CLOB account version and select signing mode.
            # Version 2 accounts use the POLY_1271 deposit wallet flow.
            sig_type, dw_addr = await self._resolve_signing_mode()

            self._signing = SigningModule(
                l1_private_key=self._config.l1_private_key,
                l2_api_key=self._config.l2_api_key,
                l2_secret=self._config.l2_secret,
                l2_passphrase=self._config.l2_passphrase,
                wallet_address=self._config.wallet_address,
                signature_type=sig_type,
                deposit_wallet_address=dw_addr,
            )
            await self._startup_wallet_checks()
            await self.reconcile()
        else:
            # Paper mode: initialise the paper simulator
            self._paper = PolymarketPaperSimulator(
                config=self._config,
                http=self._http,
            )
            await self._paper.__aenter__()

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Close HTTP client and paper simulator."""
        if self._paper is not None:
            await self._paper.__aexit__(exc_type, exc_val, exc_tb)
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------
    # Signing mode resolution
    # ------------------------------------------------------------------

    async def _resolve_signing_mode(self) -> tuple[int, str | None]:
        """Query the CLOB to determine the account's signing version.

        Returns:
            (signature_type, deposit_wallet_address):
            - (3, dw_addr) for V2 POLY_1271 accounts (deposit wallet flow).
            - (configured_sig_type, None) for V1 / EOA accounts.
        """
        assert self._http is not None
        try:
            r = await self._http.get(f"{self._config.clob_host}/version")
            version: int = r.json().get("version", 1) if r.status_code == 200 else 1
        except Exception:
            version = 1

        if version == 2 and self._config.deposit_wallet_address:
            logger.info(
                "polymarket_v2_account_detected",
                extra={"deposit_wallet": self._config.deposit_wallet_address},
            )
            return 3, self._config.deposit_wallet_address

        return self._config.signature_type, self._config.deposit_wallet_address

    # ------------------------------------------------------------------
    # Startup checks (live mode only)
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception(_is_retryable_rpc_exception),
        stop=stop_after_attempt(_RPC_RETRY_ATTEMPTS),
        wait=wait_exponential(
            multiplier=_RPC_RETRY_WAIT_INITIAL_SECONDS,
            min=_RPC_RETRY_WAIT_INITIAL_SECONDS,
            max=_RPC_RETRY_WAIT_MAX_SECONDS,
        ),
        reraise=True,
    )
    async def _rpc_eth_call(self, rpc_url: str, to: str, data: str) -> str:
        """Issue a JSON-RPC eth_call and return the raw hex result string.

        Retries a small number of times with exponential backoff on transient
        failures (network errors, timeouts, HTTP 429/5xx).  Does NOT retry on
        401/403 — those indicate a bad RPC URL/auth, not a transient failure,
        and retrying would only delay the caller's (intentional) fail-open
        handling in :meth:`_startup_wallet_checks`.
        """
        assert self._http is not None
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        }
        resp = await self._http.post(rpc_url, json=payload, timeout=10.0)
        resp.raise_for_status()
        return str(resp.json()["result"])

    async def _rpc_erc20_uint256(
        self, rpc_url: str, contract: str, selector_hex: str, *addr_args: str
    ) -> int:
        """Call an ERC-20 view that returns uint256; ABI-encode address args."""
        encoded_args = "".join(a.lower().removeprefix("0x").zfill(64) for a in addr_args)
        data = selector_hex + encoded_args
        result = await self._rpc_eth_call(rpc_url, contract, data)
        return int(result, 16) if result and result != "0x" else 0

    async def _startup_wallet_checks(self) -> None:
        """Perform on-chain safety checks before allowing live trading.

        1. Signature type consistency (pure, no RPC).
        2. USDC allowance cap — refuses startup if allowance > max_allowance_usdc.
        3. USDC balance ceiling — warns if balance > balance_ceiling_usdc.

        RPC failures log at warning and do NOT block startup (best-effort).
        """
        cfg = self._config

        # 1. Signature type consistency
        if cfg.signature_type == 3 and cfg.deposit_wallet_address is None:
            raise RuntimeError(
                "signature_type=3 (POLY_1271) requires deposit_wallet_address to be set"
            )
        logger.info(
            "polymarket_sig_type_ok",
            extra={"signature_type": cfg.signature_type},
        )

        # 2. USDC allowance check
        try:
            raw_allowance = await self._rpc_erc20_uint256(
                cfg.polygon_rpc_url,
                _USDC_POLYGON,
                "0xdd62ed3e",  # allowance(address,address)
                cfg.wallet_address,
                _CTF_EXCHANGE,
            )
            allowance_usdc = Decimal(raw_allowance) / Decimal("1000000")
            logger.info(
                "polymarket_usdc_allowance_checked",
                extra={"allowance_usdc": str(allowance_usdc), "cap": str(cfg.max_allowance_usdc)},
            )
            if allowance_usdc > cfg.max_allowance_usdc:
                raise RuntimeError(
                    f"On-chain USDC allowance {allowance_usdc} exceeds cap "
                    f"{cfg.max_allowance_usdc}. Revoke or reduce before trading."
                )
        except RuntimeError:
            raise
        except Exception as exc:
            logger.warning(
                "polymarket_allowance_check_failed",
                extra={"error": str(exc)},
            )

        # 3. USDC balance ceiling (warn only — does not block)
        try:
            raw_balance = await self._rpc_erc20_uint256(
                cfg.polygon_rpc_url,
                _USDC_POLYGON,
                "0x70a08231",  # balanceOf(address)
                cfg.wallet_address,
            )
            balance_usdc = Decimal(raw_balance) / Decimal("1000000")
            logger.info(
                "polymarket_usdc_balance_checked",
                extra={"balance_usdc": str(balance_usdc), "ceiling": str(cfg.balance_ceiling_usdc)},
            )
            if balance_usdc > cfg.balance_ceiling_usdc:
                logger.warning(
                    "polymarket_hot_wallet_balance_high",
                    extra={
                        "balance_usdc": str(balance_usdc),
                        "ceiling": str(cfg.balance_ceiling_usdc),
                    },
                )
        except Exception as exc:
            logger.warning(
                "polymarket_balance_check_failed",
                extra={"error": str(exc)},
            )

    # ------------------------------------------------------------------
    # place
    # ------------------------------------------------------------------

    async def place(self, intent: OrderIntent) -> OrderResult:
        """Submit an order to Polymarket or the paper simulator.

        Args:
            intent: Fully-formed order intent with deterministic client_order_id.

        Returns:
            :class:`~src.exchanges.base.OrderResult` reflecting acceptance/rejection.
        """
        if self._reconcile_failed:
            return OrderResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=None,
                accepted=False,
                reason="Placement refused: reconciliation failed. Call reconcile() first.",
                raw={"error": "reconcile_failed"},
            )

        if self._mode == Mode.PAPER:
            assert self._paper is not None, "Paper simulator not initialised"
            return await self._paper.place(intent)

        return await self._place_live(intent)

    async def _check_geoblock(self) -> dict[str, Any] | None:
        """Query Polymarket's own geoblock check immediately before an order.

        ``https://polymarket.com/api/geoblock`` is the same check the
        Polymarket frontend uses to decide whether to show the
        region-restricted banner. It's on a different host than the CLOB
        order-placement endpoint (``clob.polymarket.com``) and may use a
        different/stricter list, so a ``blocked: false`` result here is a
        useful diagnostic signal but not a guarantee ``POST /order`` will be
        accepted — logged for correlation, not treated as authoritative.

        Returns:
            The parsed JSON body (``{"blocked": bool, "ip": str,
            "country": str, "region": str}``), or ``None`` if the check
            itself failed. A failed check never blocks order placement —
            this is diagnostic-only, not a hard gate.
        """
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get("https://polymarket.com/api/geoblock")
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                return data
        except Exception as exc:
            logger.debug("polymarket_geoblock_check_failed", extra={"error": str(exc)})
            return None

    async def _place_live(self, intent: OrderIntent) -> OrderResult:
        """Build, sign, and submit an EIP-712 order to the CLOB."""
        assert self._http is not None
        assert self._signing is not None

        geoblock = await self._check_geoblock()
        if geoblock is not None:
            logger.info("polymarket_geoblock_precheck", extra=geoblock)
            if geoblock.get("blocked") is True:
                return OrderResult(
                    client_order_id=intent.client_order_id,
                    exchange_order_id=None,
                    accepted=False,
                    reason=(
                        f"[auth] Pre-flight geoblock check reported blocked=true "
                        f"(ip={geoblock.get('ip')}, country={geoblock.get('country')}) "
                        "— order not submitted."
                    ),
                    raw={"error": "geoblock_precheck", **geoblock},
                )

        try:
            signed = self._signing.sign_order(intent)
        except Exception as exc:
            logger.error("polymarket_sign_order_failed", extra={"error": str(exc)})
            return OrderResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=None,
                accepted=False,
                reason=f"Signing failed: {exc}",
                raw={"error": "signing_failed"},
            )

        wire = signed.to_wire()
        body_str = json.dumps(
            {
                "order": wire,
                "owner": self._config.l2_api_key,
                "orderType": "GTC",
                "postOnly": False,
                "deferExec": False,
            }
        )
        logger.info("polymarket_place_wire_payload", extra={"payload": body_str})
        headers = {
            "Content-Type": "application/json",
            **self._signing.build_l2_headers("POST", "/order", body_str),
        }

        try:
            # Deliberately NOT self._http (the adapter's shared, process-lifetime
            # client): that client stays alive across every tick via frequent
            # read-only polling, so its underlying TCP connection can persist
            # for the bot's entire runtime. If it was established through a
            # VPN/proxy exit IP that Polymarket's fraud detection flags, every
            # order placed over that same connection inherits the same
            # rejection for as long as the process lives — no amount of
            # retrying escapes it. A dedicated short-lived client here forces
            # a brand-new TCP connection (and thus a new egress IP, when
            # traffic is routed through a rotating VPN/proxy) on every single
            # order attempt, independent of the shared client's connection
            # lifetime — matching scripts/snipe_bot.py's per-invocation-fresh-
            # process behavior, which does not exhibit this failure mode.
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as fresh_client:
                resp = await fresh_client.post(
                    f"{self._config.clob_host}/order",
                    content=body_str.encode(),
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            logger.warning("polymarket_place_timeout", extra={"error": str(exc)})
            return OrderResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=None,
                accepted=False,
                reason=f"Timeout: {exc}",
                raw={"error": "timeout"},
            )
        except httpx.RequestError as exc:
            logger.warning("polymarket_place_network_error", extra={"error": str(exc)})
            return OrderResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=None,
                accepted=False,
                reason=f"Network error: {exc}",
                raw={"error": "network_error"},
            )

        return self._parse_place_response(intent.client_order_id, resp)

    def _parse_place_response(
        self,
        client_order_id: str,
        resp: httpx.Response,
    ) -> OrderResult:
        """Parse a CLOB POST /order response into a typed OrderResult.

        Branches on Content-Type to handle JSON vs plain-text errors.
        Classifies errors as retryable/fatal/auth/not_found.

        Args:
            client_order_id: The id of the order being placed.
            resp: Raw httpx response.

        Returns:
            :class:`~src.exchanges.base.OrderResult`.
        """
        status = resp.status_code
        content_type = resp.headers.get("content-type", "")
        is_json = "application/json" in content_type

        if status == 200 or status == 201:
            try:
                body: dict[str, Any] = resp.json()
            except Exception:
                body = {"raw": resp.text}
            exchange_order_id: str | None = body.get("orderID") or body.get("order_id")
            if exchange_order_id:
                self._inflight[client_order_id] = exchange_order_id
            return OrderResult(
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                accepted=True,
                reason=None,
                raw=body,
            )

        # Error path — parse body for human-readable reason
        if is_json:
            try:
                err_body: dict[str, Any] = resp.json()
            except Exception:
                err_body = {"raw": resp.text}
            reason = err_body.get("error") or err_body.get("message") or str(err_body)
        else:
            reason = resp.text or f"HTTP {status}"
            err_body = {"raw": resp.text, "status": status}

        category = self._classify_error(status, is_json, reason)
        logger.warning(
            "polymarket_place_rejected",
            extra={"status": status, "category": category, "reason": reason[:200]},
        )
        return OrderResult(
            client_order_id=client_order_id,
            exchange_order_id=None,
            accepted=False,
            reason=f"[{category}] {reason}",
            raw=err_body,
        )

    @staticmethod
    def _classify_error(status: int, is_json: bool, body: str) -> str:
        """Classify an error response into a routing category.

        Args:
            status: HTTP status code.
            is_json: Whether the response had a JSON Content-Type.
            body: Parsed error message or raw body text.

        Returns:
            One of ``"retryable"``, ``"fatal"``, ``"auth"``, ``"not_found"``.
        """
        if status in (429, 503, 504):
            return "retryable"
        if status in (401, 403):
            return "auth"
        if status == 404:
            return "not_found"
        return "fatal"

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------

    async def cancel(self, client_order_id: str) -> bool:
        """Cancel a single order.  Idempotent — 404 / already-cancelled → False.

        Args:
            client_order_id: The deterministic id used when placing.

        Returns:
            ``True`` if the exchange confirmed cancellation; ``False`` if already
            gone (idempotent).
        """
        if self._mode == Mode.PAPER:
            assert self._paper is not None
            return await self._paper.cancel(client_order_id)

        exchange_order_id = self._inflight.get(client_order_id)
        if exchange_order_id is None:
            # We don't know the exchange id — nothing to cancel
            logger.debug(
                "polymarket_cancel_unknown",
                extra={"client_order_id": client_order_id},
            )
            return False

        assert self._http is not None
        assert self._signing is not None

        body_str = json.dumps({"orderID": exchange_order_id})
        headers = self._signing.build_l2_headers("DELETE", "/order", body_str)

        try:
            resp = await self._http.request(
                "DELETE",
                f"{self._config.clob_host}/order",
                content=body_str.encode(),
                headers=headers,
            )
        except httpx.RequestError as exc:
            logger.warning("polymarket_cancel_network_error", extra={"error": str(exc)})
            return False

        if resp.status_code in (200, 204):
            self._inflight.pop(client_order_id, None)
            return True
        if resp.status_code == 404:
            self._inflight.pop(client_order_id, None)
            return False

        logger.warning(
            "polymarket_cancel_unexpected_status",
            extra={"status": resp.status_code, "body": resp.text[:200]},
        )
        return False

    # ------------------------------------------------------------------
    # cancel_all
    # ------------------------------------------------------------------

    async def cancel_all(self, market_id: str | None = None) -> int:
        """Cancel all open orders, optionally scoped to one market.

        Args:
            market_id: Condition ID to scope the cancellation, or ``None``
                for a global cancel.

        Returns:
            Number of orders cancelled (best-effort from exchange response).
        """
        if self._mode == Mode.PAPER:
            assert self._paper is not None
            return await self._paper.cancel_all(market_id)

        assert self._http is not None
        assert self._signing is not None

        if market_id is not None:
            path = f"/cancel-market-orders?market={market_id}"
            headers = self._signing.build_l2_headers("DELETE", path)
            try:
                resp = await self._http.delete(
                    f"{self._config.clob_host}/cancel-market-orders",
                    params={"market": market_id},
                    headers=headers,
                )
            except httpx.RequestError as exc:
                logger.warning("polymarket_cancel_all_network_error", extra={"error": str(exc)})
                return 0
        else:
            path = "/cancel-all"
            headers = self._signing.build_l2_headers("DELETE", path)
            try:
                resp = await self._http.delete(
                    f"{self._config.clob_host}/cancel-all",
                    headers=headers,
                )
            except httpx.RequestError as exc:
                logger.warning("polymarket_cancel_all_network_error", extra={"error": str(exc)})
                return 0

        if resp.status_code not in (200, 204):
            logger.warning(
                "polymarket_cancel_all_error",
                extra={"status": resp.status_code, "body": resp.text[:200]},
            )
            return 0

        # Clear in-flight cache entries for the cancelled market (or all)
        if market_id is not None:
            # We can't easily filter by market without more bookkeeping;
            # clear all as a conservative approach
            count = len(self._inflight)
            self._inflight.clear()
        else:
            count = len(self._inflight)
            self._inflight.clear()

        # Try to parse count from response
        try:
            body = resp.json()
            if isinstance(body, dict):
                parsed_count: int | None = body.get("count") or body.get("canceled")
                if isinstance(parsed_count, int):
                    return parsed_count
        except Exception:
            pass

        return count

    # ------------------------------------------------------------------
    # get_market
    # ------------------------------------------------------------------

    async def get_market(self, market_id: str) -> Market:
        """Fetch market metadata from the CLOB.  Results are cached 60 seconds.

        Args:
            market_id: Condition ID for the market.

        Returns:
            :class:`~src.exchanges.base.Market` with tick_size and min_size
            populated from the CLOB.
        """
        cached = self._market_cache.get(market_id)
        if cached is not None:
            market_obj, fetched_at = cached
            if (datetime.now(tz=UTC) - fetched_at).total_seconds() < self._MARKET_CACHE_TTL:
                return market_obj

        assert self._http is not None

        try:
            resp = await self._http.get(
                f"{self._config.clob_host}/markets/{market_id}",
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "polymarket_get_market_http_error",
                extra={"market_id": market_id, "status": exc.response.status_code},
            )
            raise
        except httpx.RequestError as exc:
            logger.warning(
                "polymarket_get_market_network_error",
                extra={"market_id": market_id, "error": str(exc)},
            )
            raise

        # Parse tick size and min order size (both arrive as decimal strings)
        tick_size = Decimal(str(data.get("minimum_tick_size", "0.01")))
        min_size = Decimal(str(data.get("minimum_order_size", "1")))

        # token_id: the condition_id is used here; caller should pass
        # the specific token_id when constructing Market objects for trading.
        # The CLOB market endpoint returns tokens list; take first for default.
        token_id = market_id
        tokens = data.get("tokens", [])
        if tokens and isinstance(tokens, list) and isinstance(tokens[0], dict):
            token_id = str(tokens[0].get("token_id", market_id))

        market_obj = Market(
            market_id=market_id,
            token_id=token_id,
            tick_size=tick_size,
            min_size=min_size,
            venue="polymarket",
        )
        self._market_cache[market_id] = (market_obj, datetime.now(tz=UTC))
        return market_obj

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def events(self) -> AsyncIterator[ExchangeEvent]:
        """Async generator producing typed exchange events from WS + Gamma polling.

        Yields:
            :class:`~src.exchanges.base.ExchangeEvent` instances.
        """
        if self._mode == Mode.PAPER:
            assert self._paper is not None
            return self._paper.events()
        return self._live_events()

    async def _live_events(self) -> AsyncGenerator[ExchangeEvent, None]:
        """Internal generator for live WS events with exponential backoff reconnect."""
        import websockets

        backoff = 1.0
        max_backoff = 60.0

        while True:
            try:
                async with websockets.connect(self._config.ws_host) as ws:
                    backoff = 1.0  # reset on successful connect
                    # Subscribe to all known token IDs
                    if self._subscribed_token_ids:
                        sub_msg = json.dumps(
                            {
                                "type": "market",
                                "assets_ids": self._subscribed_token_ids,
                            }
                        )
                        await ws.send(sub_msg)

                    async for raw_msg in ws:
                        if not isinstance(raw_msg, str):
                            continue
                        try:
                            payload = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            logger.debug(
                                "polymarket_ws_invalid_json",
                                extra={"raw": raw_msg[:200]},
                            )
                            continue

                        # Branch: list = initial snapshot; dict = update
                        if isinstance(payload, list):
                            async for event in self._dispatch_snapshot(payload):
                                yield event
                        elif isinstance(payload, dict):
                            maybe_event = self._dispatch_update(payload)
                            if maybe_event is not None:
                                yield maybe_event

            except Exception as exc:
                logger.warning(
                    "polymarket_ws_disconnected",
                    extra={"error": str(exc), "backoff": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _dispatch_snapshot(
        self,
        items: list[dict[str, Any]],
    ) -> AsyncGenerator[ExchangeEvent, None]:
        """Parse a WS list-shaped initial snapshot.

        List payloads from Polymarket have no ``event_type`` field — they
        represent the initial book state.  We emit nothing for now
        (book state is consumed by the paper simulator or tracked internally).

        Args:
            items: List of order-book entries from the initial snapshot message.

        Yields:
            Nothing (snapshot items update internal book state only).
        """
        # Snapshot items are used for book initialisation (in paper mode).
        # In live mode, the adapter does not maintain its own book.
        # Yield nothing — callers will implement book tracking if needed.
        for _item in items:
            pass
        return
        yield  # pragma: no cover  # make this a valid async generator function

    def _dispatch_update(self, payload: dict[str, Any]) -> ExchangeEvent | None:
        """Parse a WS object-shaped update message.

        Branches on ``event_type`` to produce the appropriate typed event.

        Args:
            payload: Single WS update message (has ``event_type``).

        Returns:
            A typed :class:`~src.exchanges.base.ExchangeEvent` or ``None`` if
            the event type is unknown/unhandled.
        """
        event_type = payload.get("event_type", "")

        if event_type in ("last_trade_price", "trade"):
            # Fill event
            try:
                return FillEvent(
                    client_order_id=payload.get("maker_order_id", ""),
                    exchange_order_id=payload.get("id", ""),
                    fill_price=Decimal(str(payload.get("price", "0"))),
                    fill_size=Decimal(str(payload.get("size", "0"))),
                    fee=Decimal(str(payload.get("fee", "0"))),
                    fill_at=_parse_timestamp(payload.get("timestamp")),
                )
            except Exception as exc:
                logger.debug("polymarket_fill_parse_error", extra={"error": str(exc)})
                return None

        if event_type == "order_cancelled":
            try:
                return CancelEvent(
                    client_order_id=payload.get("owner_id", ""),
                    exchange_order_id=payload.get("id", ""),
                    cancelled_at=_parse_timestamp(payload.get("timestamp")),
                    requested=False,
                )
            except Exception as exc:
                logger.debug("polymarket_cancel_parse_error", extra={"error": str(exc)})
                return None

        if event_type == "order_rejected":
            try:
                return RejectionEvent(
                    client_order_id=payload.get("owner_id", ""),
                    reason=payload.get("reason", "unknown"),
                    category="fatal",
                    rejected_at=_parse_timestamp(payload.get("timestamp")),
                )
            except Exception as exc:
                logger.debug("polymarket_rejection_parse_error", extra={"error": str(exc)})
                return None

        logger.debug(
            "polymarket_ws_unhandled_event_type",
            extra={"event_type": event_type},
        )
        return None

    # ------------------------------------------------------------------
    # reconcile
    # ------------------------------------------------------------------

    async def reconcile(self) -> None:
        """Reconcile in-memory inflight cache against the CLOB's open orders.

        Pulls open orders from GET /data/orders, diffs against _inflight,
        and adopts the exchange's view as authoritative.

        If the exchange is unreachable, sets ``_reconcile_failed = True``,
        which blocks ``place()`` until reconciliation succeeds.
        """
        if self._mode == Mode.PAPER:
            return

        assert self._http is not None
        assert self._signing is not None

        path = "/data/orders"
        headers = self._signing.build_l2_headers("GET", path)

        try:
            resp = await self._http.get(
                f"{self._config.clob_host}/data/orders",
                params={"owner": self._config.wallet_address, "status": "LIVE"},
                headers=headers,
            )
            resp.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.warning(
                "polymarket_reconcile_failed",
                extra={"error": str(exc)},
            )
            self._reconcile_failed = True
            return

        try:
            raw = resp.json()
            # API returns either a list or a paginated dict {"data": [...]}
            orders: list[dict[str, Any]] = raw if isinstance(raw, list) else raw.get("data", [])
        except Exception as exc:
            logger.warning(
                "polymarket_reconcile_parse_failed",
                extra={"error": str(exc)},
            )
            self._reconcile_failed = True
            return

        # Build exchange view: exchange_order_id -> order
        exchange_ids = {o.get("id", "") for o in orders if o.get("id")}

        # Diff: anything in _inflight not in exchange is stale
        stale = [coid for coid, eid in self._inflight.items() if eid not in exchange_ids]
        if stale:
            logger.warning(
                "polymarket_reconcile_stale_orders",
                extra={"stale_client_order_ids": stale},
            )
            for coid in stale:
                self._inflight.pop(coid, None)

        # Adopt exchange's open orders (we may not have the client_order_id)
        # Mark reconciliation as successful
        self._reconcile_failed = False
        logger.info("polymarket_reconcile_complete", extra={"open_order_count": len(orders)})

    # ------------------------------------------------------------------
    # Settlement polling
    # ------------------------------------------------------------------

    async def poll_settlement(self, market_id: str, token_id: str) -> SettlementEvent | None:
        """Poll Gamma for settlement of a specific market.

        Called periodically (every 60 s) to detect resolved markets.

        Args:
            market_id: Condition ID.
            token_id: ERC-1155 token ID the strategy holds.

        Returns:
            :class:`~src.exchanges.base.SettlementEvent` if resolved, else ``None``.
        """
        assert self._http is not None

        try:
            resp = await self._http.get(
                f"{self._config.gamma_host}/markets/{market_id}",
                params={"closed": "true"},
            )
        except httpx.RequestError:
            return None

        if resp.status_code != 200:
            return None

        try:
            data: dict[str, Any] = resp.json()
        except Exception:
            return None

        if not data.get("closed", False):
            return None

        # Parse Gamma JSON-string-encoded arrays
        try:
            clob_token_ids: list[str] = json.loads(data.get("clobTokenIds", "[]"))
            outcome_prices: list[str] = json.loads(data.get("outcomePrices", "[]"))
        except (json.JSONDecodeError, TypeError):
            logger.warning("polymarket_settlement_parse_error", extra={"market_id": market_id})
            return None

        try:
            idx = clob_token_ids.index(token_id)
            payout = Decimal(outcome_prices[idx])
        except (ValueError, IndexError):
            logger.warning(
                "polymarket_settlement_token_not_found",
                extra={"market_id": market_id, "token_id": token_id},
            )
            return None

        # Parse closedTime: Gamma uses space format "2026-03-19 23:20:15+00"
        closed_time_raw = data.get("closedTime", data.get("updatedAt", ""))
        settled_at = _parse_gamma_timestamp(closed_time_raw)

        resolver = ""
        uma_statuses = data.get("umaResolutionStatuses", "[]")
        try:
            parsed_statuses = json.loads(uma_statuses)
            if parsed_statuses:
                resolver = str(parsed_statuses[0])
        except (json.JSONDecodeError, TypeError, IndexError):
            pass

        return SettlementEvent(
            market_id=market_id,
            token_id=token_id,
            payout=payout,
            settled_at=settled_at,
            resolver=resolver,
            raw=dict(data),
        )

    # ------------------------------------------------------------------
    # Subscription helpers
    # ------------------------------------------------------------------

    def subscribe_token(self, token_id: str) -> None:
        """Register a token ID for WS event subscriptions.

        Call this before entering the context manager to have the adapter
        automatically subscribe to the token's events on WS connect.

        Args:
            token_id: ERC-1155 outcome token ID to subscribe to.
        """
        if token_id not in self._subscribed_token_ids:
            self._subscribed_token_ids.append(token_id)
            if self._paper is not None:
                self._paper.subscribe_token(token_id)


# ---------------------------------------------------------------------------
# Paper simulator
# ---------------------------------------------------------------------------


class PolymarketPaperSimulator:
    """In-process paper-mode simulator backed by live Polymarket WS data.

    Maintains an in-memory order book from live price_change events.
    On place(): taking orders fill immediately at far touch; resting orders
    fill when the book trades through.

    Args:
        config: :class:`PolymarketConfig` for WS connection.
        http: Shared :class:`httpx.AsyncClient` instance.
        latency_ms: Optional simulated latency in milliseconds.
    """

    def __init__(
        self,
        config: PolymarketConfig,
        http: httpx.AsyncClient,
        latency_ms: float = 0.0,
    ) -> None:
        self._config = config
        self._http = http
        self._latency_ms = latency_ms

        # Best bid/ask per token_id
        self._best_bid: dict[str, Decimal] = {}
        self._best_ask: dict[str, Decimal] = {}

        # Open paper orders: client_order_id -> (intent, placed_at)
        self._open_orders: dict[str, OrderIntent] = {}

        # Exchange order id mapping
        self._exchange_ids: dict[str, str] = {}

        # Event queue
        self._event_queue: asyncio.Queue[ExchangeEvent] = asyncio.Queue()

        # Subscribed token IDs
        self._subscribed_token_ids: list[str] = []

        # Fee schedule (loaded from Gamma at startup)
        self._taker_fee_bps: int = 50  # 5% taker default
        self._maker_rebate_bps: int = 25  # 25bp maker rebate default

        # WS background task
        self._ws_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> PolymarketPaperSimulator:
        """Start WS background task."""
        self._ws_task = asyncio.create_task(self._ws_loop())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Cancel WS background task."""
        import contextlib

        if self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task

    def subscribe_token(self, token_id: str) -> None:
        """Register a token ID for book tracking."""
        if token_id not in self._subscribed_token_ids:
            self._subscribed_token_ids.append(token_id)

    async def place(self, intent: OrderIntent) -> OrderResult:
        """Simulate order placement against the live book.

        Taking orders (aggressive price) fill immediately at far touch.
        Resting orders are added to the pending book.

        Args:
            intent: Fully-formed order intent.

        Returns:
            :class:`~src.exchanges.base.OrderResult`.
        """
        import uuid as _uuid

        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1000)

        exchange_order_id = f"paper-{_uuid.uuid4().hex[:12]}"
        self._open_orders[intent.client_order_id] = intent
        self._exchange_ids[intent.client_order_id] = exchange_order_id

        token_id = intent.market.token_id
        best_ask = self._best_ask.get(token_id, Decimal("1"))
        best_bid = self._best_bid.get(token_id, Decimal("0"))

        is_taking = (intent.side == Side.BUY and intent.price >= best_ask) or (
            intent.side == Side.SELL and intent.price <= best_bid
        )

        if is_taking:
            fill_price = best_ask if intent.side == Side.BUY else best_bid
            fee = self._compute_taker_fee(intent.price, intent.size)
            fill_event = FillEvent(
                client_order_id=intent.client_order_id,
                exchange_order_id=exchange_order_id,
                fill_price=fill_price,
                fill_size=intent.size,
                fee=fee,
                fill_at=datetime.now(tz=UTC),
            )
            self._event_queue.put_nowait(fill_event)
            self._open_orders.pop(intent.client_order_id, None)

        return OrderResult(
            client_order_id=intent.client_order_id,
            exchange_order_id=exchange_order_id,
            accepted=True,
            reason=None,
            raw={"paper": "accepted", "is_taking": is_taking},
        )

    async def cancel(self, client_order_id: str) -> bool:
        """Cancel a paper order.

        Args:
            client_order_id: The id used when placing.

        Returns:
            ``True`` if cancelled; ``False`` if not found.
        """
        if client_order_id not in self._open_orders:
            return False
        self._open_orders.pop(client_order_id)
        exchange_order_id = self._exchange_ids.pop(client_order_id, "")
        self._event_queue.put_nowait(
            CancelEvent(
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                cancelled_at=datetime.now(tz=UTC),
                requested=True,
            )
        )
        return True

    async def cancel_all(self, market_id: str | None = None) -> int:
        """Cancel all open paper orders.

        Args:
            market_id: If supplied, cancel only orders for this market.

        Returns:
            Number of orders cancelled.
        """
        to_cancel = [
            coid
            for coid, intent in self._open_orders.items()
            if market_id is None or intent.market.market_id == market_id
        ]
        count = 0
        for coid in to_cancel:
            if await self.cancel(coid):
                count += 1
        return count

    def events(self) -> AsyncIterator[ExchangeEvent]:
        """Drain the paper event queue.

        Yields:
            :class:`~src.exchanges.base.ExchangeEvent` as they occur.
        """
        return self._events_gen()

    async def _events_gen(self) -> AsyncGenerator[ExchangeEvent, None]:
        """Internal async generator backing events()."""
        while True:
            event = await self._event_queue.get()
            yield event

    def _compute_taker_fee(self, price: Decimal, size: Decimal) -> Decimal:
        """Compute taker fee based on the current fee schedule.

        Args:
            price: Order price.
            size: Order size.

        Returns:
            Fee amount in USDC.
        """
        notional = price * size
        return notional * Decimal(self._taker_fee_bps) / Decimal("10000")

    def update_book(self, token_id: str, best_bid: Decimal, best_ask: Decimal) -> None:
        """Update the in-memory book and check for resting order fills.

        Args:
            token_id: The token whose book has updated.
            best_bid: New best bid price.
            best_ask: New best ask price.
        """
        self._best_bid[token_id] = best_bid
        self._best_ask[token_id] = best_ask

        # Check resting orders for fills
        filled = []
        for coid, intent in self._open_orders.items():
            if intent.market.token_id != token_id:
                continue
            fills_now = (intent.side == Side.BUY and intent.price >= best_ask) or (
                intent.side == Side.SELL and intent.price <= best_bid
            )
            if fills_now:
                fill_price = best_ask if intent.side == Side.BUY else best_bid
                fee = self._compute_taker_fee(intent.price, intent.size)
                exchange_order_id = self._exchange_ids.get(coid, "")
                self._event_queue.put_nowait(
                    FillEvent(
                        client_order_id=coid,
                        exchange_order_id=exchange_order_id,
                        fill_price=fill_price,
                        fill_size=intent.size,
                        fee=fee,
                        fill_at=datetime.now(tz=UTC),
                    )
                )
                filled.append(coid)

        for coid in filled:
            self._open_orders.pop(coid, None)

    async def _ws_loop(self) -> None:
        """Background task: subscribe to WS and update the live book."""
        import websockets

        backoff = 1.0
        max_backoff = 60.0

        while True:
            try:
                async with websockets.connect(self._config.ws_host) as ws:
                    backoff = 1.0
                    if self._subscribed_token_ids:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "market",
                                    "assets_ids": self._subscribed_token_ids,
                                }
                            )
                        )

                    async for raw_msg in ws:
                        if not isinstance(raw_msg, str):
                            continue
                        try:
                            payload = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            continue

                        if isinstance(payload, list):
                            # Initial snapshot — extract best bid/ask
                            for item in payload:
                                self._handle_snapshot_item(item)
                        elif isinstance(payload, dict):
                            self._handle_ws_update(payload)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("paper_ws_disconnected", extra={"error": str(exc)})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    def _handle_snapshot_item(self, item: dict[str, Any]) -> None:
        """Parse one entry from the WS initial snapshot list."""
        token_id = item.get("asset_id", "")
        if not token_id:
            return
        bids = item.get("bids", [])
        asks = item.get("asks", [])
        if bids:
            best_bid = max(Decimal(str(b.get("price", "0"))) for b in bids)
            self._best_bid[token_id] = best_bid
        if asks:
            best_ask = min(Decimal(str(a.get("price", "1"))) for a in asks)
            self._best_ask[token_id] = best_ask

    def _handle_ws_update(self, payload: dict[str, Any]) -> None:
        """Process a WS update dict and update the book."""
        event_type = payload.get("event_type", "")
        token_id = payload.get("asset_id", "")

        if event_type == "price_change" and token_id:
            bids = payload.get("bids", [])
            asks = payload.get("asks", [])
            new_bid = self._best_bid.get(token_id, Decimal("0"))
            new_ask = self._best_ask.get(token_id, Decimal("1"))
            if bids:
                new_bid = max(Decimal(str(b.get("price", "0"))) for b in bids)
            if asks:
                new_ask = min(Decimal(str(a.get("price", "1"))) for a in asks)
            self.update_book(token_id, new_bid, new_ask)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(raw: object) -> datetime:
    """Parse a timestamp value from WS payloads into a UTC datetime.

    Handles Unix seconds (int/float), Unix milliseconds (large int), and
    ISO-8601 strings.

    Args:
        raw: Raw timestamp value from the WS payload.

    Returns:
        UTC-aware :class:`datetime`.
    """
    if raw is None:
        return datetime.now(tz=UTC)
    if isinstance(raw, (int, float)):
        ts = float(raw)
        # Heuristic: > 1e12 means milliseconds
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace(" ", "T"))
        except ValueError:
            pass
    return datetime.now(tz=UTC)


def _parse_gamma_timestamp(raw: str | None) -> datetime:
    """Parse Gamma's space-separated timestamp format.

    Gamma uses: ``"2026-03-19 23:20:15+00"`` — replace space with T.

    Args:
        raw: Raw timestamp string from Gamma API.

    Returns:
        UTC-aware :class:`datetime`.
    """
    if not raw:
        return datetime.now(tz=UTC)
    try:
        return datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        return datetime.now(tz=UTC)


def load_polymarket_config(secrets_slice: dict[str, Any]) -> PolymarketConfig:
    """Build a :class:`PolymarketConfig` from a secrets slice.

    Accepts the dict under the ``polymarket`` key of ``exchanges.enc.yaml``.
    If it contains a ``live`` sub-key, that sub-dict is used (allows future
    ``paper`` sub-key for separate paper credentials).

    Field name aliases handled here:
    - ``l2_api_secret`` → ``l2_secret`` (secrets file uses the longer name)

    Deposit wallet auto-derivation:
    - If ``deposit_wallet_address`` is not in secrets, it is derived
      deterministically from ``wallet_address`` using the Polymarket deposit
      wallet factory (CREATE2 math, no network call required).
    - If the derived address is present in secrets, the secrets value takes
      precedence (allows overriding for non-standard setups).

    Args:
        secrets_slice: Dict loaded from ``secrets/exchanges.enc.yaml``
            under the ``polymarket`` key.

    Returns:
        Validated :class:`PolymarketConfig`.
    """
    data = dict(secrets_slice.get("live", secrets_slice))

    # Alias: secrets YAML uses l2_api_secret; PolymarketConfig uses l2_secret
    if "l2_api_secret" in data and "l2_secret" not in data:
        data["l2_secret"] = data.pop("l2_api_secret")

    # YAML parses unquoted hex values as integers — coerce back to 0x strings.
    for field in ("l1_private_key", "wallet_address"):
        if field in data and isinstance(data[field], int):
            data[field] = hex(data[field])

    # Auto-derive deposit wallet address if not explicitly provided.
    # Pure CREATE2 math — no network call.
    if "deposit_wallet_address" not in data and "wallet_address" in data:
        try:
            from py_builder_relayer_client.builder.derive import (  # type: ignore[import-untyped]
                derive_uups_deposit_wallet,
            )
            from py_builder_relayer_client.config import (  # type: ignore[import-untyped]
                get_contract_config as _get_relayer_cfg,
            )

            _rc = _get_relayer_cfg(137)
            data["deposit_wallet_address"] = derive_uups_deposit_wallet(
                data["wallet_address"],
                _rc.deposit_wallet_factory,
                _rc.deposit_wallet_implementation,
            )
        except Exception:
            pass  # derivation unavailable; leave as None

    return PolymarketConfig(**data)
