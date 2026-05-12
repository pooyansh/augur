"""BTC/USD 15-minute price signal — Coingecko (primary) + Binance (fallback).

Signal shape (output of :meth:`Btc15Min.parse`)::

    {
        "price_usd": "67234.50",   # Decimal-serialised as string
        "source": "coingecko",     # or "binance"
        "source_ts": "2026-05-09T12:00:00+00:00",  # ISO-8601 UTC
    }

Coingecko free tier: ~30 req/min; we use a 0.5 req/s token bucket (burst=2).
Binance public API has generous limits; no rate limiter applied.
"""

from __future__ import annotations

__all__ = ["Btc15Min", "BtcBinanceSource", "BtcCoingeckoSource"]

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

import httpx

from src.signals.base import Signal, SignalSource
from src.signals.ratelimit import TokenBucket
from src.signals.registry import signal

logger = logging.getLogger(__name__)

# Module-level token bucket shared by all Coingecko source instances.
# 0.5 req/s sustained, burst of 2 — respects the free-tier limit.
_coingecko_bucket: TokenBucket = TokenBucket(rate_per_sec=0.5, burst=2)

_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
_BINANCE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"


class BtcCoingeckoSource(SignalSource):
    """Fetch BTC/USD price from the Coingecko public API.

    Rate-limited via a module-level :class:`~src.signals.ratelimit.TokenBucket`
    (0.5 req/s sustained, burst 2) to respect the free tier.

    Expected raw response::

        {"bitcoin": {"usd": 67234.50}}
    """

    name: ClassVar[str] = "coingecko"

    async def fetch(self, params: Mapping[str, Any]) -> Any:
        """Fetch current BTC/USD price from Coingecko.

        Args:
            params: Ignored — Coingecko source has no configurable params.

        Returns:
            Parsed JSON dict from the Coingecko API.

        Raises:
            httpx.HTTPStatusError: On non-2xx responses (including 429).
            httpx.RequestError: On network/timeout errors.
        """
        await _coingecko_bucket.acquire()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = client.build_request("GET", _COINGECKO_URL)
            response = await client.send(resp)
            response.raise_for_status()
            return response.json()


class BtcBinanceSource(SignalSource):
    """Fetch BTC/USD price from the Binance public REST API.

    No authentication required.  No rate limiter — Binance public endpoints
    allow 1200 requests per minute by default.

    Expected raw response::

        {"symbol": "BTCUSDT", "price": "67234.50000000"}
    """

    name: ClassVar[str] = "binance"

    async def fetch(self, params: Mapping[str, Any]) -> Any:
        """Fetch current BTC/USDT price from Binance.

        Args:
            params: Ignored — Binance source has no configurable params.

        Returns:
            Parsed JSON dict from the Binance API.

        Raises:
            httpx.HTTPStatusError: On non-2xx responses.
            httpx.RequestError: On network/timeout errors.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(_BINANCE_URL)
            response.raise_for_status()
            return response.json()


@signal
class Btc15Min(Signal):
    """15-minute BTC/USD price signal.

    Cadence: 900 s (15 min).  Freshness tolerance: 1800 s (30 min) — two
    missed cadences before the signal is considered stale.

    Sources (in order):
    1. :class:`BtcCoingeckoSource` — primary (rate-limited free tier).
    2. :class:`BtcBinanceSource` — fallback (public, no auth).

    Canonical payload shape::

        {
            "price_usd": "67234.50",   # Decimal-serialised string
            "source": "coingecko",     # or "binance"
            "source_ts": "2026-05-09T12:00:00+00:00",
        }
    """

    name: ClassVar[str] = "btc_15min"
    cadence_seconds: ClassVar[int] = 900
    tolerance_seconds: ClassVar[int] = 1800
    sources: ClassVar[list[type[SignalSource]]] = [BtcCoingeckoSource, BtcBinanceSource]

    def parse(self, source_name: str, raw: Any) -> dict[str, Any]:
        """Parse a raw source response into the canonical BTC price shape.

        Args:
            source_name: ``"coingecko"`` or ``"binance"``.
            raw: The raw JSON dict returned by the source.

        Returns:
            Canonical dict with ``price_usd``, ``source``, and ``source_ts``.

        Raises:
            ValueError: If the raw response cannot be parsed.
        """
        source_ts = datetime.now(tz=UTC).isoformat()

        if source_name == "coingecko":
            try:
                price_raw = raw["bitcoin"]["usd"]
                price = Decimal(str(price_raw))
            except (KeyError, TypeError, InvalidOperation) as exc:
                raise ValueError(f"Coingecko parse error: {exc}") from exc

        elif source_name == "binance":
            try:
                price = Decimal(raw["price"])
            except (KeyError, TypeError, InvalidOperation) as exc:
                raise ValueError(f"Binance parse error: {exc}") from exc

        else:
            raise ValueError(f"Unknown source '{source_name}' for btc_15min")

        return {
            "price_usd": str(price),
            "source": source_name,
            "source_ts": source_ts,
        }
