"""Token-bucket rate limiter for signal sources.

Rate-limit handling lives in the runner / source layer, not inside
``Signal`` subclasses.  ``SignalSource`` implementations that need to respect
a per-second rate limit (e.g. Coingecko free tier) acquire a token before
calling their endpoint.

Usage::

    _bucket = TokenBucket(rate_per_sec=0.5, burst=2)

    async def fetch(self, params):
        await _bucket.acquire()
        return await http.get(URL)
"""

from __future__ import annotations

__all__ = ["TokenBucket"]

import asyncio
import time


class TokenBucket:
    """Async token-bucket rate limiter.

    Tokens refill continuously at ``rate_per_sec`` up to ``burst`` capacity.
    ``acquire()`` waits until a token is available, then consumes one.

    This implementation is **not** thread-safe — it is designed for use inside
    a single asyncio event loop.

    Args:
        rate_per_sec: Sustained token-refill rate (tokens per second).
            E.g. ``0.5`` = one request every 2 seconds sustained.
        burst: Maximum token capacity (initial fill level).
            E.g. ``2`` allows two back-to-back requests before throttling.
    """

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        if rate_per_sec <= 0:
            raise ValueError(f"rate_per_sec must be positive, got {rate_per_sec}")
        if burst < 1:
            raise ValueError(f"burst must be >= 1, got {burst}")
        self._rate = rate_per_sec
        self._burst = float(burst)
        self._tokens: float = float(burst)  # start full
        self._last_refill: float = time.monotonic()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    async def acquire(self) -> None:
        """Wait until a token is available, then consume one.

        Returns immediately if a token is available.  Suspends the coroutine
        for the minimum wait time if the bucket is empty.
        """
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # Calculate how long until one token is available.
            wait_s = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait_s)
