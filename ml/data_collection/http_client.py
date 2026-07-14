"""Retryable, rate-limited HTTP client for public read-only market data APIs.

This is a small, dependency-light equivalent of ``src/signals/ratelimit.py``'s
``TokenBucket`` shape, duplicated here (not imported from ``src/``) because
``ml/`` must remain an independent sibling package that ``src/`` never
depends on and that never depends on ``src/`` internals either.
"""

from __future__ import annotations

__all__ = [
    "GammaHttpClient",
    "RetryableHttpClient",
    "RetryableStatusError",
    "TokenBucket",
    "TradesApiHttpClient",
]

import time
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


class TokenBucket:
    """Synchronous (blocking) token-bucket rate limiter.

    Tokens refill continuously at ``rate_per_sec`` up to ``burst`` capacity.
    ``acquire()`` blocks the calling thread until a token is available. This
    mirrors ``src/signals/ratelimit.TokenBucket`` but uses ``time.sleep``
    instead of ``asyncio.sleep`` since the collector is a synchronous batch
    job, not an event-loop service.

    Args:
        rate_per_sec: Sustained token-refill rate (tokens per second).
        burst: Maximum token capacity (initial fill level).

    Raises:
        ValueError: If ``rate_per_sec`` is not positive or ``burst`` < 1.
    """

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        if rate_per_sec <= 0:
            raise ValueError(f"rate_per_sec must be positive, got {rate_per_sec}")
        if burst < 1:
            raise ValueError(f"burst must be >= 1, got {burst}")
        self._rate = rate_per_sec
        self._burst = float(burst)
        self._tokens: float = float(burst)
        self._last_refill: float = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait_s = (1.0 - self._tokens) / self._rate
            time.sleep(wait_s)


class RetryableStatusError(RuntimeError):
    """Raised for HTTP responses that should trigger a retry (429 / 5xx)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TransportError | RetryableStatusError)


class RetryableHttpClient:
    """Retryable, rate-limited GET client shared by all public read-only APIs
    this package talks to (Gamma, the Data API, ...).

    Non-2xx/3xx responses other than 429/5xx (e.g. 404, 422) are surfaced as
    ``httpx.HTTPStatusError`` immediately — those are deterministic API
    responses (bad request, "offset too large", etc.), not transient
    failures, so callers must handle them explicitly rather than have them
    silently retried away.

    Args:
        base_url: API root, e.g. ``https://gamma-api.polymarket.com``.
        rate_per_sec: Sustained request rate.
        burst: Token bucket burst capacity.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        *,
        rate_per_sec: float = 5.0,
        burst: int = 5,
        timeout: float = 10.0,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._bucket = TokenBucket(rate_per_sec=rate_per_sec, burst=burst)

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def get(self, path: str, params: dict[str, Any]) -> Any:
        """Issue a rate-limited, retried GET and return the parsed JSON body.

        Args:
            path: Path relative to ``base_url``, e.g. ``"/events"``.
            params: Query parameters.

        Returns:
            The parsed JSON response body (list or dict, per endpoint).

        Raises:
            httpx.HTTPStatusError: For non-retryable HTTP error responses
                (4xx other than 429, or after retries are exhausted).
        """
        self._bucket.acquire()
        return self._get_with_retry(path, params)

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    def _get_with_retry(self, path: str, params: dict[str, Any]) -> Any:
        response = self._client.get(path, params=params)
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableStatusError(response.status_code, response.text)
        response.raise_for_status()
        return response.json()


class GammaHttpClient(RetryableHttpClient):
    """Retryable, rate-limited GET client for the Gamma public API.

    Gamma has no published public rate limit; ``5.0`` req/s is a conservative
    default.
    """

    def __init__(
        self,
        base_url: str = "https://gamma-api.polymarket.com",
        *,
        rate_per_sec: float = 5.0,
        burst: int = 5,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(base_url, rate_per_sec=rate_per_sec, burst=burst, timeout=timeout)


class TradesApiHttpClient(RetryableHttpClient):
    """Retryable, rate-limited GET client for ``data-api.polymarket.com``.

    This host has no documented public rate limit either (unlike Gamma, we
    have no prior operational experience with it at all). ``5.0`` req/s is a
    conservative starting default, not a value verified against a published
    limit — back off (lower ``rate_per_sec``) if 429s are observed in
    practice.
    """

    def __init__(
        self,
        base_url: str = "https://data-api.polymarket.com",
        *,
        rate_per_sec: float = 5.0,
        burst: int = 5,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(base_url, rate_per_sec=rate_per_sec, burst=burst, timeout=timeout)
