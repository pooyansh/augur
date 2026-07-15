"""BTC/USD fast price signal — Coingecko (primary) + Binance (fallback).

Same sources and canonical shape as :mod:`src.signals.btc_15min`, but a
20s cadence instead of 900s. ``btc_15min`` was reused by
``polymarket.btc_up_or_down_5m.price_compare`` for the provisional
winning-rule fast path and found live to be structurally inert there: a
15-minute refresh cadence can't detect a price move within a 5-minute
market window — the "live" sample is frequently still the exact same one
captured at entry, so the rule always reads a ~0% move and never commits
to WON/LOST. This signal exists specifically for that fast-path use case
on 5-minute-or-shorter windows; ``btc_15min`` remains correct/cheaper for
anything on a 15-minute-or-longer cadence (e.g. momentum_v1).

Signal shape (output of :meth:`BtcFast.parse`)::

    {
        "price_usd": "67234.50",   # Decimal-serialised as string
        "source": "coingecko",     # or "binance"
        "source_ts": "2026-05-09T12:00:00+00:00",  # ISO-8601 UTC
    }

Coingecko free tier: ~30 req/min. At 20s cadence this signal alone uses
3 req/min — the runner's dedup guarantee (one fetch loop per unique
(signal_name, params_hash), shared across every subscribing bot) keeps
this well within the free tier's 0.5 req/s token bucket regardless of
subscriber count.
"""

from __future__ import annotations

__all__ = ["BtcFast"]

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from src.signals.base import Signal, SignalSource
from src.signals.btc_15min import BtcBinanceSource, BtcCoingeckoSource
from src.signals.registry import signal


@signal
class BtcFast(Signal):
    """Fast-cadence BTC/USD price signal, for sub-15-minute market windows.

    Cadence: 20 s. Freshness tolerance: 60 s — three missed cadences
    before the signal is considered stale, short enough to matter within
    a 5-minute window rather than silently going quiet for most of it.

    Sources (in order) — reuses btc_15min's source implementations
    unchanged, only the cadence/name/tolerance differ:
    1. :class:`~src.signals.btc_15min.BtcCoingeckoSource` — primary.
    2. :class:`~src.signals.btc_15min.BtcBinanceSource` — fallback.

    Canonical payload shape::

        {
            "price_usd": "67234.50",   # Decimal-serialised string
            "source": "coingecko",     # or "binance"
            "source_ts": "2026-05-09T12:00:00+00:00",
        }
    """

    name: ClassVar[str] = "btc_fast"
    cadence_seconds: ClassVar[int] = 20
    tolerance_seconds: ClassVar[int] = 60
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
            raise ValueError(f"Unknown source '{source_name}' for btc_fast")

        return {
            "price_usd": str(price),
            "source": source_name,
            "source_ts": source_ts,
        }
