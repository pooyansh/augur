"""Resolve a MarketRef to a concrete Market, fetching from the venue API if needed.

Static markets (market_id + token_id) only need a CLOB metadata fetch for
tick_size and min_size.  Dynamic/recurring markets (slug + outcome) compute the
current window timestamp and query the Gamma API to find the active condition_id
and token_ids, then fall back to the next window if the current one is not yet
accepting orders.
"""

from __future__ import annotations

__all__ = ["ResolvedMarket", "resolve_all_outcomes", "resolve_market"]

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

from src.exchanges.base import Market

if TYPE_CHECKING:
    from src.manager.config import MarketRef

_GAMMA_HOST = "https://gamma-api.polymarket.com"
_CLOB_HOST = "https://clob.polymarket.com"
_WINDOW_SECONDS = 900  # 15-minute recurring market cadence


def resolve_market(ref: MarketRef, *, http_timeout: float = 10.0) -> Market:
    """Resolve a :class:`~src.manager.config.MarketRef` to a concrete :class:`Market`.

    For static refs (``market_id`` + ``token_id``) the CLOB is queried only for
    tick_size / min_size metadata.  For dynamic refs (``slug`` + ``outcome``) the
    current and next window timestamps are tried in order until one is found that
    is open and accepting orders.

    Args:
        ref: Validated market reference from ``config/bots.yaml``.
        http_timeout: Seconds before any single HTTP request times out.

    Returns:
        Fully populated :class:`Market` ready for use by an adapter.

    Raises:
        RuntimeError: If no accepting market window is found for a slug-based ref.
        httpx.HTTPError: On unrecoverable network or HTTP errors.
    """
    if ref.slug is not None:
        return _resolve_slug(ref, http_timeout=http_timeout)
    assert ref.market_id is not None and ref.token_id is not None
    tick_size, min_size = _fetch_clob_metadata(ref.market_id, http_timeout=http_timeout)
    return Market(
        market_id=ref.market_id,
        token_id=ref.token_id,
        tick_size=tick_size,
        min_size=min_size,
        venue=ref.exchange,
    )


def _resolve_slug(ref: MarketRef, *, http_timeout: float) -> Market:
    assert ref.slug is not None and ref.outcome is not None
    now = int(time.time())
    current = (now // _WINDOW_SECONDS) * _WINDOW_SECONDS
    for window_ts in [current, current + _WINDOW_SECONDS, current + 2 * _WINDOW_SECONDS]:
        full_slug = f"{ref.slug}-{window_ts}"
        try:
            market = _try_slug(full_slug, ref.outcome, ref.exchange, http_timeout=http_timeout)
        except httpx.HTTPError:
            continue
        if market is not None:
            return market
    raise RuntimeError(
        f"No active market found for slug pattern {ref.slug!r} "
        f"(tried 3 windows starting at ts={current})"
    )


def _try_slug(slug: str, outcome: str, exchange: str, *, http_timeout: float) -> Market | None:
    with httpx.Client(timeout=http_timeout) as client:
        r = client.get(f"{_GAMMA_HOST}/events", params={"slug": slug})
        r.raise_for_status()
    events: list[dict] = r.json() if isinstance(r.json(), list) else [r.json()]
    if not events:
        return None
    event = events[0]
    markets = event.get("markets", [])
    if not markets:
        return None
    m = markets[0]
    if m.get("closed") or not m.get("acceptingOrders"):
        return None
    condition_id: str = m.get("conditionId", "")
    raw_ids = m.get("clobTokenIds", "[]")
    token_ids: list[str] = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
    token_id = _pick_token(token_ids, outcome, m)
    if not token_id:
        raise ValueError(
            f"Outcome {outcome!r} not found in market {slug!r}. "
            f"Token IDs: {token_ids}, outcomes field: {m.get('outcomes')}"
        )
    tick_size, min_size = _fetch_clob_metadata(condition_id, http_timeout=http_timeout)
    return Market(
        market_id=condition_id,
        token_id=token_id,
        tick_size=tick_size,
        min_size=min_size,
        venue=exchange,
    )


def _pick_token(token_ids: list[str], outcome: str, market: dict) -> str | None:
    """Match an outcome name to its ERC-1155 token ID.

    Tries the ``outcomes`` field from the Gamma market response first (name
    matching), then falls back to position conventions (UP/YES → index 0,
    DOWN/NO → index 1) for binary markets.
    """
    raw_outcomes = market.get("outcomes", "[]")
    try:
        outcomes: list[str] = (
            json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else list(raw_outcomes)
        )
    except (json.JSONDecodeError, TypeError):
        outcomes = []

    outcome_upper = outcome.upper()
    for i, name in enumerate(outcomes):
        if isinstance(name, str) and name.upper() == outcome_upper and i < len(token_ids):
            return token_ids[i]

    # Positional fallback for binary markets
    if len(token_ids) == 2:
        if outcome_upper in ("UP", "YES"):
            return token_ids[0]
        if outcome_upper in ("DOWN", "NO"):
            return token_ids[1]

    return None


@dataclass
class ResolvedMarket:
    """All outcome tokens resolved for a market (not just the configured one).

    Args:
        condition_id: Polymarket ``condition_id`` (hex string).
        slug: Full slug including the window timestamp (e.g.
            ``"btc-updown-15m-1780764300"``).  Empty string for static markets.
        tokens: Mapping of ``outcome_name → token_id``.  Outcome names are
            exact strings from Polymarket (e.g. ``"Up"``, ``"Down"``).
        window_end: Unix timestamp when the current window closes.  ``None``
            for non-recurring (static) markets.
        tick_size: Minimum price increment from the CLOB.
        min_size: Minimum order size from the CLOB.
    """

    condition_id: str
    slug: str
    tokens: dict[str, str]
    window_end: float | None
    tick_size: Decimal
    min_size: Decimal


def resolve_all_outcomes(ref: MarketRef, *, http_timeout: float = 10.0) -> ResolvedMarket:
    """Resolve all outcome tokens for a market (not just the configured one).

    For slug-based (dynamic) refs the Gamma API is queried for the current
    active window; if that window is closed the next window is tried.  All
    outcome tokens are returned in a dict keyed by their exact Polymarket
    outcome name.

    For static refs (``market_id`` + ``token_id``) only the one configured
    token is available via this ref.  A single-entry ``tokens`` dict is
    returned keyed by ``token_id`` (the outcome name is unknown without an
    additional Gamma lookup).

    Args:
        ref: Validated market reference from ``config/bots.yaml``.
        http_timeout: Seconds before any single HTTP request times out.

    Returns:
        :class:`ResolvedMarket` with all outcome tokens.

    Raises:
        RuntimeError: If no accepting market window is found for a slug ref.
        httpx.HTTPError: On unrecoverable network or HTTP errors.
    """
    if ref.slug is not None:
        return _resolve_all_slug(ref, http_timeout=http_timeout)

    # Static ref — only one token is configured; outcome name is unknown
    assert ref.market_id is not None and ref.token_id is not None
    tick_size, min_size = _fetch_clob_metadata(ref.market_id, http_timeout=http_timeout)
    return ResolvedMarket(
        condition_id=ref.market_id,
        slug="",
        # Use the token_id as the outcome name — caller knows this is a static ref
        tokens={ref.token_id: ref.token_id},
        window_end=None,
        tick_size=tick_size,
        min_size=min_size,
    )


def _resolve_all_slug(ref: MarketRef, *, http_timeout: float) -> ResolvedMarket:
    """Resolve all outcome tokens for a slug-based market ref.

    Args:
        ref: Must have ``slug`` set; ``outcome`` may be ``None``.
        http_timeout: Per-request HTTP timeout in seconds.

    Returns:
        :class:`ResolvedMarket` with all outcomes from the active window.

    Raises:
        RuntimeError: If no active window is found within the 3-window search.
    """
    assert ref.slug is not None
    now = int(time.time())
    current = (now // _WINDOW_SECONDS) * _WINDOW_SECONDS
    for window_ts in [current, current + _WINDOW_SECONDS, current + 2 * _WINDOW_SECONDS]:
        full_slug = f"{ref.slug}-{window_ts}"
        try:
            result = _try_all_slug(
                full_slug,
                window_ts,
                ref.exchange,
                http_timeout=http_timeout,
            )
        except httpx.HTTPError:
            continue
        if result is not None:
            return result
    raise RuntimeError(
        f"No active market found for slug pattern {ref.slug!r} "
        f"(tried 3 windows starting at ts={current})"
    )


def _try_all_slug(
    slug: str, window_ts: int, exchange: str, *, http_timeout: float
) -> ResolvedMarket | None:
    """Attempt to resolve all outcome tokens for a specific full slug.

    Returns ``None`` if the market is closed or not accepting orders.

    Args:
        slug: Full slug with window timestamp suffix.
        window_ts: The window start timestamp embedded in the slug.
        exchange: Exchange venue name (e.g. ``"polymarket"``).
        http_timeout: Per-request HTTP timeout in seconds.

    Returns:
        :class:`ResolvedMarket` or ``None``.
    """
    with httpx.Client(timeout=http_timeout) as client:
        r = client.get(f"{_GAMMA_HOST}/events", params={"slug": slug})
        r.raise_for_status()
    events: list[dict] = r.json() if isinstance(r.json(), list) else [r.json()]
    if not events:
        return None
    event = events[0]
    markets = event.get("markets", [])
    if not markets:
        return None
    m = markets[0]
    if m.get("closed") or not m.get("acceptingOrders"):
        return None

    condition_id: str = m.get("conditionId", "")
    raw_ids = m.get("clobTokenIds", "[]")
    raw_outcomes = m.get("outcomes", "[]")
    token_ids: list[str] = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
    outcomes: list[str] = (
        json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else list(raw_outcomes)
    )

    # Build outcome_name → token_id mapping using Polymarket's exact strings
    tokens: dict[str, str] = dict(zip(outcomes, token_ids, strict=False))

    tick_size, min_size = _fetch_clob_metadata(condition_id, http_timeout=http_timeout)
    return ResolvedMarket(
        condition_id=condition_id,
        slug=slug,
        tokens=tokens,
        window_end=float(window_ts + _WINDOW_SECONDS),
        tick_size=tick_size,
        min_size=min_size,
    )


def _fetch_clob_metadata(condition_id: str, *, http_timeout: float) -> tuple[Decimal, Decimal]:
    with httpx.Client(timeout=http_timeout) as client:
        r = client.get(f"{_CLOB_HOST}/markets/{condition_id}")
        r.raise_for_status()
    data: dict = r.json()
    tick_size = Decimal(str(data.get("minimum_tick_size", "0.01")))
    min_size = Decimal(str(data.get("minimum_order_size", "5")))
    return tick_size, min_size
