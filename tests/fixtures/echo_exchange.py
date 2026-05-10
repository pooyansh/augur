"""EchoExchange — in-memory adapter for unit tests and property suites.

Behaviour:
- ``place()`` returns ``OrderResult(accepted=True, exchange_order_id=uuid)``
  by default.  Configure ``reject=True`` to flip to rejected.
- ``cancel()`` removes the order from the in-memory book; a second cancel of
  the same id returns ``False`` (idempotency — mirrors the Polymarket open
  question resolved conservatively: second cancel = noop/False).
- ``events()`` drains an ``asyncio.Queue`` so tests can inject events.
"""

from __future__ import annotations

__all__ = ["EchoExchange"]

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal

from src.exchanges.base import (
    CancelEvent,
    ExchangeAdapter,
    ExchangeEvent,
    FillEvent,
    Market,
    Mode,
    OrderIntent,
    OrderResult,
)


class EchoExchange(ExchangeAdapter):
    """In-memory :class:`~src.exchanges.base.ExchangeAdapter` for tests.

    Args:
        mode: Execution mode (always behaves as paper regardless of value).
        reject: If ``True``, every ``place()`` call returns a rejected result.
        fill_after_place: If ``True``, a :class:`~src.exchanges.base.FillEvent`
            is enqueued immediately after every accepted placement.
    """

    venue: str = "echo"

    def __init__(
        self,
        mode: Mode = Mode.PAPER,
        *,
        reject: bool = False,
        fill_after_place: bool = False,
    ) -> None:
        super().__init__(mode)
        self._reject = reject
        self._fill_after_place = fill_after_place

        # client_order_id -> exchange_order_id
        self._open_orders: dict[str, str] = {}
        self._event_queue: asyncio.Queue[ExchangeEvent] = asyncio.Queue()

        # Counters for test assertions
        self.place_call_count: int = 0
        self.cancel_call_count: int = 0

    async def place(self, intent: OrderIntent) -> OrderResult:
        """Accept or reject the intent and record it in the in-memory book.

        Args:
            intent: The order to place.

        Returns:
            :class:`~src.exchanges.base.OrderResult` with ``accepted=True``
            unless configured with ``reject=True``.
        """
        self.place_call_count += 1

        if self._reject:
            return OrderResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=None,
                accepted=False,
                reason="EchoExchange configured to reject",
                raw={"echo": "rejected"},
            )

        exchange_order_id = str(uuid.uuid4())
        self._open_orders[intent.client_order_id] = exchange_order_id

        if self._fill_after_place:
            fill = FillEvent(
                client_order_id=intent.client_order_id,
                exchange_order_id=exchange_order_id,
                fill_price=intent.price,
                fill_size=intent.size,
                fee=Decimal("0"),
                fill_at=datetime.now(tz=UTC),
            )
            self._event_queue.put_nowait(fill)

        return OrderResult(
            client_order_id=intent.client_order_id,
            exchange_order_id=exchange_order_id,
            accepted=True,
            reason=None,
            raw={"echo": "accepted", "exchange_order_id": exchange_order_id},
        )

    async def cancel(self, client_order_id: str) -> bool:
        """Remove an order from the in-memory book.

        A second cancel of the same id returns ``False`` (idempotent).

        Args:
            client_order_id: The id used when placing the order.

        Returns:
            ``True`` if found and cancelled; ``False`` if already gone.
        """
        self.cancel_call_count += 1

        if client_order_id not in self._open_orders:
            return False

        exchange_order_id = self._open_orders.pop(client_order_id)
        event = CancelEvent(
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            cancelled_at=datetime.now(tz=UTC),
            requested=True,
        )
        self._event_queue.put_nowait(event)
        return True

    async def cancel_all(self, market_id: str | None = None) -> int:
        """Cancel all open orders (market_id is ignored in the echo adapter).

        Args:
            market_id: Ignored.

        Returns:
            Number of orders cancelled.
        """
        ids = list(self._open_orders.keys())
        count = 0
        for coid in ids:
            cancelled = await self.cancel(coid)
            if cancelled:
                count += 1
        return count

    async def get_market(self, market_id: str) -> Market:
        """Return a minimal echo market.

        Args:
            market_id: Identifier echoed back in the ``Market.market_id`` field.

        Returns:
            A :class:`~src.exchanges.base.Market` with dummy parameters.
        """
        return Market(
            market_id=market_id,
            token_id="echo-token-0",
            tick_size=Decimal("0.01"),
            min_size=Decimal("1"),
            venue="echo",
        )

    async def enqueue_event(self, event: ExchangeEvent) -> None:
        """Inject an event into the queue (test helper).

        Args:
            event: Any :data:`~src.exchanges.base.ExchangeEvent` to deliver.
        """
        self._event_queue.put_nowait(event)

    def events(self) -> AsyncIterator[ExchangeEvent]:
        """Async generator that drains the internal event queue.

        Yields:
            :class:`~src.exchanges.base.ExchangeEvent` in insertion order.
            Blocks (``await``s) when the queue is empty.
        """
        return self._events_gen()

    async def _events_gen(self) -> AsyncIterator[ExchangeEvent]:  # type: ignore[override]
        """Internal async generator backing :meth:`events`."""
        while True:
            event = await self._event_queue.get()
            yield event

    @property
    def open_orders(self) -> Mapping[str, str]:
        """Read-only view of ``{client_order_id: exchange_order_id}``."""
        return self._open_orders
