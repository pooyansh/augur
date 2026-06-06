"""Live CLOB price feed for Polymarket outcome tokens.

Subscribes to the Polymarket WebSocket feed and optionally polls the REST
``/book`` endpoint as a fallback.  Yields :class:`PriceUpdate` events to the
caller via an async iterator.

Usage::

    tokens = {"Up": "71321045...", "Down": "98723456..."}
    async with ClobPriceFeed(tokens) as feed:
        async for update in feed:
            print(update.outcome, update.bid, update.ask)
"""

from __future__ import annotations

__all__ = ["ClobPriceFeed", "PriceUpdate"]

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import httpx

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_CLOB_HOST = "https://clob.polymarket.com"

logger = logging.getLogger(__name__)


@dataclass
class PriceUpdate:
    """A single bid/ask snapshot for one outcome token.

    Args:
        token_id: ERC-1155 token ID string.
        outcome: Exact outcome name as returned by Polymarket (e.g. ``"Up"``,
            ``"Down"``, ``"Yes"``, ``"No"``).  Never normalised.
        bid: Best bid price as a :class:`~decimal.Decimal`.
        ask: Best ask price as a :class:`~decimal.Decimal`.
        source: Feed source that produced this update.
        ts: UTC timestamp of the update.
    """

    token_id: str
    outcome: str
    bid: Decimal
    ask: Decimal
    source: Literal["ws_snapshot", "ws_price_change", "rest_poll"]
    ts: datetime


def _parse_best_bid(bids: list[dict]) -> Decimal | None:
    """Extract best bid from a bids list (sorted ascending; last = best).

    Args:
        bids: List of price-level dicts each containing a ``"price"`` key.

    Returns:
        Best bid as :class:`~decimal.Decimal`, or ``None`` if ``bids`` is empty.
    """
    if not bids:
        return None
    try:
        return Decimal(str(bids[-1]["price"]))
    except (KeyError, ValueError, TypeError):
        return None


def _parse_best_ask(asks: list[dict]) -> Decimal | None:
    """Extract best ask from an asks list (sorted descending; last = best).

    Args:
        asks: List of price-level dicts each containing a ``"price"`` key.

    Returns:
        Best ask as :class:`~decimal.Decimal`, or ``None`` if ``asks`` is empty.
    """
    if not asks:
        return None
    try:
        return Decimal(str(asks[-1]["price"]))
    except (KeyError, ValueError, TypeError):
        return None


class ClobPriceFeed:
    """Subscribe to one or more CLOB tokens and yield :class:`PriceUpdate` events.

    Runs two concurrent asyncio tasks:

    * A WebSocket subscriber that receives real-time ``price_change`` events
      from the Polymarket WS feed.  Reconnects with exponential backoff on any
      error.
    * An optional REST poller that fetches ``/book`` for each token every
      ``rest_poll_interval`` seconds.  Emits a :class:`PriceUpdate` only when
      the best ask has changed since the last poll.

    Both tasks enqueue :class:`PriceUpdate` items into a shared
    :class:`asyncio.Queue`.  The async iterator drains that queue.

    Args:
        tokens: Mapping of ``outcome_name → token_id``.  Outcome strings are
            used as-is from Polymarket (never normalised).
        rest_poll_interval: Seconds between REST ``/book`` polls.
            Set to ``0.0`` to disable REST polling.
    """

    def __init__(
        self,
        tokens: dict[str, str],
        *,
        rest_poll_interval: float = 15.0,
    ) -> None:
        self._tokens = tokens  # outcome_name → token_id
        self._token_to_outcome: dict[str, str] = {v: k for k, v in tokens.items()}
        self._rest_poll_interval = rest_poll_interval
        self._queue: asyncio.Queue[PriceUpdate] = asyncio.Queue()
        self._ws_task: asyncio.Task[None] | None = None
        self._rest_task: asyncio.Task[None] | None = None
        # Last-seen ask per token — used by REST poller to detect changes
        self._last_ask: dict[str, Decimal] = {}

    async def __aenter__(self) -> ClobPriceFeed:
        """Start the WS subscriber and REST poller tasks.

        Returns:
            Self, so the object can be used directly in an ``async with`` block.
        """
        self._ws_task = asyncio.create_task(self._ws_loop(), name="clob_ws_loop")
        if self._rest_poll_interval > 0:
            self._rest_task = asyncio.create_task(self._rest_poll_loop(), name="clob_rest_poll")
        return self

    async def __aexit__(self, *_: object) -> None:
        """Cancel both background tasks cleanly."""
        for task in (self._ws_task, self._rest_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._ws_task = None
        self._rest_task = None

    def __aiter__(self) -> AsyncIterator[PriceUpdate]:
        """Return self as an async iterator of :class:`PriceUpdate` events."""
        return self._iter()

    async def _iter(self) -> AsyncIterator[PriceUpdate]:
        """Drain the internal queue, yielding each :class:`PriceUpdate`.

        Yields:
            :class:`PriceUpdate` items as they arrive from the WS or REST tasks.
        """
        while True:
            update = await self._queue.get()
            yield update

    # ------------------------------------------------------------------
    # WebSocket loop
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        """Subscribe to the Polymarket WS feed with exponential-backoff reconnect.

        Reconnect strategy: start at 1 s, double on each failure, cap at 60 s.
        Reset to 1 s on any successful connection.
        """
        import websockets

        backoff = 1.0
        max_backoff = 60.0
        token_ids = list(self._token_to_outcome.keys())

        while True:
            try:
                async with websockets.connect(_WS_URL) as ws:
                    backoff = 1.0  # reset on successful connect
                    sub_msg = json.dumps({"type": "market", "assets_ids": token_ids})
                    await ws.send(sub_msg)
                    logger.debug(
                        "clob_feed_ws_connected",
                        extra={"token_count": len(token_ids)},
                    )

                    async for raw in ws:
                        if not isinstance(raw, str):
                            continue
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        self._process_ws_payload(payload)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "clob_feed_ws_disconnected",
                    extra={"error": str(exc), "backoff": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    def _process_ws_payload(self, payload: object) -> None:
        """Parse a raw WS payload and enqueue :class:`PriceUpdate` items.

        Handles both list (initial snapshot) and dict (incremental update)
        payload shapes from the Polymarket WS protocol.

        Args:
            payload: Parsed JSON from the WS connection.
        """
        now = datetime.now(tz=UTC)

        if isinstance(payload, list):
            # Initial book snapshot — list of per-token items, no event_type
            for item in payload:
                token_id = item.get("asset_id", "")
                if token_id not in self._token_to_outcome:
                    continue
                bids: list[dict] = item.get("bids", [])
                asks: list[dict] = item.get("asks", [])
                bid = _parse_best_bid(bids)
                ask = _parse_best_ask(asks)
                if bid is None or ask is None:
                    continue
                self._last_ask[token_id] = ask
                self._queue.put_nowait(
                    PriceUpdate(
                        token_id=token_id,
                        outcome=self._token_to_outcome[token_id],
                        bid=bid,
                        ask=ask,
                        source="ws_snapshot",
                        ts=now,
                    )
                )

        elif isinstance(payload, dict):
            event_type = payload.get("event_type", "")
            token_id = payload.get("asset_id", "")

            if event_type == "price_change" and token_id in self._token_to_outcome:
                bids = payload.get("bids", [])
                asks = payload.get("asks", [])
                bid = _parse_best_bid(bids)
                ask = _parse_best_ask(asks)
                if bid is None or ask is None:
                    return
                self._last_ask[token_id] = ask
                self._queue.put_nowait(
                    PriceUpdate(
                        token_id=token_id,
                        outcome=self._token_to_outcome[token_id],
                        bid=bid,
                        ask=ask,
                        source="ws_price_change",
                        ts=now,
                    )
                )

            elif event_type in ("last_trade_price", "trade"):
                # Fill events — log only, not a PriceUpdate
                logger.debug(
                    "clob_feed_trade",
                    extra={
                        "token_id": token_id,
                        "price": payload.get("price"),
                        "size": payload.get("size"),
                    },
                )

    # ------------------------------------------------------------------
    # REST poll loop
    # ------------------------------------------------------------------

    async def _rest_poll_loop(self) -> None:
        """Poll ``/book`` for each token every ``rest_poll_interval`` seconds.

        Emits a :class:`PriceUpdate` only when the best ask has changed since
        the previous poll for that token.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                await asyncio.sleep(self._rest_poll_interval)
                for token_id, outcome in self._token_to_outcome.items():
                    try:
                        r = await client.get(
                            f"{_CLOB_HOST}/book",
                            params={"token_id": token_id},
                        )
                        r.raise_for_status()
                        data = r.json()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.debug(
                            "clob_feed_rest_poll_error",
                            extra={"token_id": token_id, "error": str(exc)},
                        )
                        continue

                    bids: list[dict] = data.get("bids", [])
                    asks: list[dict] = data.get("asks", [])
                    bid = _parse_best_bid(bids)
                    ask = _parse_best_ask(asks)
                    if bid is None or ask is None:
                        continue

                    prev_ask = self._last_ask.get(token_id)
                    if prev_ask is not None and ask == prev_ask:
                        continue  # no change — skip

                    self._last_ask[token_id] = ask
                    self._queue.put_nowait(
                        PriceUpdate(
                            token_id=token_id,
                            outcome=outcome,
                            bid=bid,
                            ask=ask,
                            source="rest_poll",
                            ts=datetime.now(tz=UTC),
                        )
                    )
