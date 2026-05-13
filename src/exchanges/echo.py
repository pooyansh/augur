"""EchoExchange — production-registered paper-mode adapter for the 'echo' venue.

This is the *registered* production adapter (venue = "echo") for use in
``config/bots.yaml`` during Phase 5.  It delegates to the same in-memory
logic as ``tests/fixtures/echo_exchange.py`` but lives in ``src/exchanges/``
so the runner's adapter factory can import it.

Polymarket adapter is deferred to Phase 4 (user chose to skip).
TODO: Add PolymarketAdapter when Phase 4 is implemented.
"""

from __future__ import annotations

__all__ = ["EchoAdapter"]

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from decimal import Decimal
from typing import ClassVar

from src.exchanges.base import (
    ExchangeAdapter,
    ExchangeEvent,
    Market,
    Mode,
    OrderIntent,
    OrderResult,
)


class EchoAdapter(ExchangeAdapter):
    """In-process paper-mode adapter for the 'echo' venue.

    Accepts all orders and returns synthetic results.  Never transmits to any
    external exchange regardless of the ``mode`` parameter.

    Args:
        mode: Execution mode.  Always behaves as paper.
    """

    venue: ClassVar[str] = "echo"

    def __init__(self, mode: Mode = Mode.PAPER) -> None:
        super().__init__(mode)
        self._open_orders: dict[str, str] = {}

    async def place(self, intent: OrderIntent) -> OrderResult:
        """Accept the order and record it in the in-memory book.

        Args:
            intent: Fully-formed order intent.

        Returns:
            Accepted :class:`~src.exchanges.base.OrderResult`.
        """
        exchange_order_id = str(uuid.uuid4())
        self._open_orders[intent.client_order_id] = exchange_order_id
        return OrderResult(
            client_order_id=intent.client_order_id,
            exchange_order_id=exchange_order_id,
            accepted=True,
            reason=None,
            raw={"echo": "accepted", "exchange_order_id": exchange_order_id},
        )

    async def cancel(self, client_order_id: str) -> bool:
        """Cancel an order from the in-memory book.

        Args:
            client_order_id: The id used when placing the order.

        Returns:
            ``True`` if cancelled; ``False`` if already gone (idempotent).
        """
        if client_order_id not in self._open_orders:
            return False
        self._open_orders.pop(client_order_id)
        return True

    async def cancel_all(self, market_id: str | None = None) -> int:
        """Cancel all open orders.

        Args:
            market_id: Ignored in the echo adapter.

        Returns:
            Number of orders cancelled.
        """
        count = len(self._open_orders)
        self._open_orders.clear()
        return count

    async def get_market(self, market_id: str) -> Market:
        """Return a synthetic echo market.

        Args:
            market_id: Identifier echoed in the returned ``Market``.

        Returns:
            :class:`~src.exchanges.base.Market` with stub parameters.
        """
        return Market(
            market_id=market_id,
            token_id="echo-token-0",
            tick_size=Decimal("0.01"),
            min_size=Decimal("1"),
            venue="echo",
        )

    def events(self) -> AsyncIterator[ExchangeEvent]:
        """Return an async generator that never yields (no-op for paper mode).

        Yields:
            Nothing — the echo adapter generates no spontaneous events.
        """
        return self._empty_events()

    async def _empty_events(self) -> AsyncGenerator[ExchangeEvent, None]:
        """Never-ending async generator with no events.

        The echo adapter has no spontaneous events; this generator sleeps
        forever without yielding.
        """
        while True:
            await asyncio.sleep(3600)
            # pragma: no cover — unreachable but required for the async generator protocol.
            yield  # type: ignore[misc]


def make_adapter(exchange: str, mode: Mode) -> ExchangeAdapter:
    """Factory that resolves an exchange name to a concrete adapter.

    Supports ``"echo"`` (in-memory simulator) and ``"polymarket"`` (Phase 4).

    Args:
        exchange: Venue name from ``MarketRef.exchange``.
        mode: Execution mode to pass to the adapter.

    Returns:
        Instantiated :class:`~src.exchanges.base.ExchangeAdapter`.

    Raises:
        NotImplementedError: If the exchange is not yet implemented.
        RuntimeError: If polymarket config cannot be loaded from secrets.
    """
    if exchange == "echo":
        return EchoAdapter(mode)

    if exchange == "polymarket":
        from src.exchanges.polymarket import PolymarketAdapter, load_polymarket_config
        from src.secrets.loader import Secrets

        secrets = Secrets.load()
        raw_config = secrets.slice_for("exchanges.polymarket")
        config = load_polymarket_config(raw_config)
        return PolymarketAdapter(mode, config)

    raise NotImplementedError(
        f"Exchange '{exchange}' is not yet implemented. Available exchanges: 'echo', 'polymarket'."
    )
