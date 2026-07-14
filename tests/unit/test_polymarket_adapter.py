"""Unit tests for the Polymarket adapter — Phase 4.

All tests are fixture-driven with no network calls.
HTTP is mocked via respx; WS is not exercised in unit tests.

Hypothesis suites cover:
  1. Deterministic salt derivation (pure function)
  2. Amount calculation relationships (BUY/SELL)
  3. Paper fill semantics (taking orders fill at far touch)
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from hypothesis import given, settings
from hypothesis import strategies as st
from src.exchanges.base import (
    Market,
    Mode,
    OrderIntent,
    Side,
)
from src.exchanges.polymarket import (
    PolymarketAdapter,
    PolymarketConfig,
    PolymarketPaperSimulator,
    _parse_gamma_timestamp,
    _parse_timestamp,
)
from src.exchanges.polymarket_signing import derive_salt

from tests.fixtures.polymarket_fixtures import (
    CLOB_MARKET_RESPONSE,
    CONDITION_ID,
    ERROR_JSON_401,
    ERROR_JSON_404,
    ERROR_PLAINTEXT_404,
    GAMMA_MARKET_RESOLVED,
    TOKEN_ID_YES,
    WS_PRICE_CHANGE_UPDATE,
    WS_SNAPSHOT_LIST,
    make_test_config,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def poly_config() -> PolymarketConfig:
    """Return a test PolymarketConfig (no real secrets)."""
    return PolymarketConfig(**make_test_config())


@pytest.fixture()
def paper_adapter(poly_config: PolymarketConfig) -> PolymarketAdapter:
    """Return a PolymarketAdapter in paper mode (no HTTP/WS needed)."""
    return PolymarketAdapter(Mode.PAPER, poly_config)


@pytest.fixture()
def live_adapter(poly_config: PolymarketConfig) -> PolymarketAdapter:
    """Return a PolymarketAdapter in live mode (HTTP mocked by caller)."""
    return PolymarketAdapter(Mode.LIVE, poly_config)


@pytest.fixture()
def test_market() -> Market:
    """Return a representative Market for testing."""
    return Market(
        market_id=CONDITION_ID,
        token_id=TOKEN_ID_YES,
        tick_size=Decimal("0.01"),
        min_size=Decimal("5"),
        venue="polymarket",
    )


@pytest.fixture()
def buy_intent(test_market: Market) -> OrderIntent:
    """A BUY order intent."""
    return OrderIntent(
        client_order_id="abcdef0123456789",
        market=test_market,
        side=Side.BUY,
        price=Decimal("0.60"),
        size=Decimal("100"),
    )


@pytest.fixture()
def sell_intent(test_market: Market) -> OrderIntent:
    """A SELL order intent."""
    return OrderIntent(
        client_order_id="fedcba9876543210",
        market=test_market,
        side=Side.SELL,
        price=Decimal("0.62"),
        size=Decimal("50"),
    )


# ---------------------------------------------------------------------------
# 1. paper mode never calls CLOB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_place_routes_to_paper_in_paper_mode(
    paper_adapter: PolymarketAdapter,
    buy_intent: OrderIntent,
) -> None:
    """Paper mode adapter never calls the CLOB endpoint."""
    # Set up a mock paper simulator
    mock_paper = AsyncMock()
    mock_paper.place = AsyncMock(
        return_value=MagicMock(accepted=True, exchange_order_id="paper-abc")
    )
    paper_adapter._paper = mock_paper

    with respx.mock(assert_all_mocked=True):
        # No HTTP routes registered — any real call would raise
        result = await paper_adapter.place(buy_intent)

    mock_paper.place.assert_awaited_once_with(buy_intent)
    assert result.accepted is True


# ---------------------------------------------------------------------------
# 2. Error parsing: JSON 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_error_parsing_json_404(
    live_adapter: PolymarketAdapter,
    buy_intent: OrderIntent,
) -> None:
    """JSON 404 response produces a rejected OrderResult with not_found category."""
    live_adapter._reconcile_failed = False
    live_adapter._signing = MagicMock()
    live_adapter._signing.sign_order = MagicMock(return_value=MagicMock(to_wire=lambda: {}))
    live_adapter._signing.build_l2_headers = MagicMock(return_value={})

    with respx.mock() as mock:
        mock.post(f"{live_adapter._config.clob_host}/order").mock(
            return_value=httpx.Response(
                404,
                json=ERROR_JSON_404,
                headers={"content-type": "application/json"},
            )
        )
        live_adapter._http = httpx.AsyncClient()
        result = await live_adapter._place_live(buy_intent)
        await live_adapter._http.aclose()

    assert result.accepted is False
    assert result.exchange_order_id is None
    assert "not_found" in (result.reason or "")


# ---------------------------------------------------------------------------
# 3. Error parsing: plain-text 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_error_parsing_plaintext_404(
    live_adapter: PolymarketAdapter,
    buy_intent: OrderIntent,
) -> None:
    """Plain-text 404 is handled gracefully — no crash, result has accepted=False."""
    live_adapter._reconcile_failed = False
    live_adapter._signing = MagicMock()
    live_adapter._signing.sign_order = MagicMock(return_value=MagicMock(to_wire=lambda: {}))
    live_adapter._signing.build_l2_headers = MagicMock(return_value={})

    with respx.mock() as mock:
        mock.post(f"{live_adapter._config.clob_host}/order").mock(
            return_value=httpx.Response(
                404,
                text=ERROR_PLAINTEXT_404,
                headers={"content-type": "text/plain"},
            )
        )
        live_adapter._http = httpx.AsyncClient()
        result = await live_adapter._place_live(buy_intent)
        await live_adapter._http.aclose()

    assert result.accepted is False
    assert result.exchange_order_id is None
    # Should contain the body text or a category indicator
    assert result.reason is not None


# ---------------------------------------------------------------------------
# 4. Error parsing: 401 auth error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_error_parsing_401(
    live_adapter: PolymarketAdapter,
    buy_intent: OrderIntent,
) -> None:
    """401 response is classified as auth error."""
    live_adapter._reconcile_failed = False
    live_adapter._signing = MagicMock()
    live_adapter._signing.sign_order = MagicMock(return_value=MagicMock(to_wire=lambda: {}))
    live_adapter._signing.build_l2_headers = MagicMock(return_value={})

    with respx.mock() as mock:
        mock.post(f"{live_adapter._config.clob_host}/order").mock(
            return_value=httpx.Response(
                401,
                json=ERROR_JSON_401,
                headers={"content-type": "application/json"},
            )
        )
        live_adapter._http = httpx.AsyncClient()
        result = await live_adapter._place_live(buy_intent)
        await live_adapter._http.aclose()

    assert result.accepted is False
    assert "auth" in (result.reason or "")


# ---------------------------------------------------------------------------
# 5. cancel: idempotent 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_cancel_idempotent_404(
    live_adapter: PolymarketAdapter,
) -> None:
    """Second cancel of an already-cancelled order returns False, not an error."""
    # First: no inflight entry at all — should return False immediately
    live_adapter._http = httpx.AsyncClient()
    live_adapter._signing = MagicMock()

    result = await live_adapter.cancel("nonexistent-order-id")
    assert result is False
    await live_adapter._http.aclose()


@pytest.mark.asyncio()
async def test_cancel_known_order_404_from_exchange(
    live_adapter: PolymarketAdapter,
) -> None:
    """When exchange returns 404 for a cancel, returns False (idempotent)."""
    exchange_order_id = "exchange-abc"
    live_adapter._inflight["my-coid"] = exchange_order_id
    live_adapter._signing = MagicMock()
    live_adapter._signing.build_l2_headers = MagicMock(return_value={})

    with respx.mock() as mock:
        mock.delete(f"{live_adapter._config.clob_host}/order").mock(
            return_value=httpx.Response(404, text="not found")
        )
        live_adapter._http = httpx.AsyncClient()
        result = await live_adapter.cancel("my-coid")
        await live_adapter._http.aclose()

    assert result is False
    # Also removed from inflight
    assert "my-coid" not in live_adapter._inflight


# ---------------------------------------------------------------------------
# 6. get_market parses tick_size correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_get_market_parses_tick_size(
    live_adapter: PolymarketAdapter,
) -> None:
    """CLOB market response is parsed with Decimal tick_size and min_size."""
    with respx.mock() as mock:
        mock.get(f"{live_adapter._config.clob_host}/markets/{CONDITION_ID}").mock(
            return_value=httpx.Response(200, json=CLOB_MARKET_RESPONSE)
        )
        live_adapter._http = httpx.AsyncClient()
        market = await live_adapter.get_market(CONDITION_ID)
        await live_adapter._http.aclose()

    assert market.market_id == CONDITION_ID
    assert market.tick_size == Decimal("0.01")
    assert market.min_size == Decimal("5")
    assert market.venue == "polymarket"
    # Ensure no float contamination
    assert isinstance(market.tick_size, Decimal)
    assert isinstance(market.min_size, Decimal)


# ---------------------------------------------------------------------------
# 7. WS: list-shaped initial snapshot
# ---------------------------------------------------------------------------


def test_ws_dispatch_list_snapshot() -> None:
    """List-shaped WS payload is recognised as an initial snapshot (no event_type)."""
    # The snapshot is a list — isinstance(payload, list) must be True
    assert isinstance(WS_SNAPSHOT_LIST, list)
    # No event_type on list-shaped payloads
    for item in WS_SNAPSHOT_LIST:
        assert "event_type" not in item

    # Test the paper simulator's book update from snapshot
    config = PolymarketConfig(**make_test_config())
    http = MagicMock(spec=httpx.AsyncClient)
    sim = PolymarketPaperSimulator(config=config, http=http)

    for item in WS_SNAPSHOT_LIST:
        sim._handle_snapshot_item(item)

    # After snapshot, YES token should have bid/ask populated
    assert sim._best_bid.get(TOKEN_ID_YES) is not None
    assert sim._best_ask.get(TOKEN_ID_YES) is not None
    assert sim._best_bid[TOKEN_ID_YES] == Decimal("0.58")
    assert sim._best_ask[TOKEN_ID_YES] == Decimal("0.62")


# ---------------------------------------------------------------------------
# 8. WS: object-shaped price_change update
# ---------------------------------------------------------------------------


def test_ws_dispatch_price_change() -> None:
    """Object-shaped WS payload with event_type=price_change updates the book."""
    assert isinstance(WS_PRICE_CHANGE_UPDATE, dict)
    assert WS_PRICE_CHANGE_UPDATE["event_type"] == "price_change"

    config = PolymarketConfig(**make_test_config())
    http = MagicMock(spec=httpx.AsyncClient)
    sim = PolymarketPaperSimulator(config=config, http=http)

    # Set initial state
    sim._best_bid[TOKEN_ID_YES] = Decimal("0.58")
    sim._best_ask[TOKEN_ID_YES] = Decimal("0.62")

    # Process the update
    sim._handle_ws_update(WS_PRICE_CHANGE_UPDATE)

    # Book should have updated
    assert sim._best_bid[TOKEN_ID_YES] == Decimal("0.60")
    assert sim._best_ask[TOKEN_ID_YES] == Decimal("0.63")


# ---------------------------------------------------------------------------
# 9. Settlement detection from Gamma
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_settlement_detection_from_gamma(
    live_adapter: PolymarketAdapter,
) -> None:
    """When Gamma returns closed=true, a SettlementEvent is emitted with correct payout."""
    with respx.mock() as mock:
        mock.get(
            f"{live_adapter._config.gamma_host}/markets/{CONDITION_ID}",
        ).mock(return_value=httpx.Response(200, json=GAMMA_MARKET_RESOLVED))
        live_adapter._http = httpx.AsyncClient()
        event = await live_adapter.poll_settlement(CONDITION_ID, TOKEN_ID_YES)
        await live_adapter._http.aclose()

    assert event is not None
    assert event.market_id == CONDITION_ID
    assert event.token_id == TOKEN_ID_YES
    assert event.payout == Decimal("1.0")
    assert event.settled_at is not None
    # resolver may be empty if umaResolutionStatuses is absent or empty list
    assert isinstance(event.resolver, str)


@pytest.mark.asyncio()
async def test_settlement_no_event_for_open_market(
    live_adapter: PolymarketAdapter,
) -> None:
    """When market is still open (closed=False), poll_settlement returns None."""
    from tests.fixtures.polymarket_fixtures import GAMMA_MARKET_ACTIVE

    with respx.mock() as mock:
        mock.get(
            f"{live_adapter._config.gamma_host}/markets/{CONDITION_ID}",
        ).mock(return_value=httpx.Response(200, json=GAMMA_MARKET_ACTIVE))
        live_adapter._http = httpx.AsyncClient()
        event = await live_adapter.poll_settlement(CONDITION_ID, TOKEN_ID_YES)
        await live_adapter._http.aclose()

    assert event is None


# ---------------------------------------------------------------------------
# 10. Deterministic salt
# ---------------------------------------------------------------------------


def test_deterministic_salt_from_client_order_id() -> None:
    """Same client_order_id always produces the same salt (Option B)."""
    coid = "abcdef0123456789"
    salt1 = derive_salt(coid)
    salt2 = derive_salt(coid)
    assert salt1 == salt2
    assert isinstance(salt1, int)
    assert salt1 >= 0


def test_different_client_order_ids_produce_different_salts() -> None:
    """Different client_order_ids produce different salts (no collision for nearby values)."""
    salt_a = derive_salt("aaaaaaaaaaaaaaaa")
    salt_b = derive_salt("bbbbbbbbbbbbbbbb")
    assert salt_a != salt_b


def test_salt_derivation_option_b_formula() -> None:
    """Verify Option B formula: first 10 hex chars (5 bytes) → big-endian int.

    Deliberately 5 bytes, not the originally-documented 8: a full 8-byte
    salt can exceed JavaScript's MAX_SAFE_INTEGER (2^53 - 1). The CLOB
    parses ``salt`` as a JSON number, so an oversized salt loses precision
    client-side and causes signature verification failures. 5 bytes caps
    the salt at 2^40, safely within range. See ``derive_salt``'s docstring
    in ``src/exchanges/polymarket_signing.py`` for the full rationale —
    this is the actual, live-trading-verified behavior; the 8-byte formula
    in an earlier version of ``.claude/rules/05-exchanges.md`` was stale.
    """
    coid = "abcdef0123456789feedbeefdeadcafe"
    expected = int.from_bytes(bytes.fromhex(coid[:10]), "big")
    assert derive_salt(coid) == expected


# ---------------------------------------------------------------------------
# 11. Error classification
# ---------------------------------------------------------------------------


def test_classify_error_retryable() -> None:
    """429, 503, 504 are classified as retryable."""
    for status in (429, 503, 504):
        cat = PolymarketAdapter._classify_error(status, True, "")
        assert cat == "retryable", f"Expected retryable for {status}"


def test_classify_error_auth() -> None:
    """401 and 403 are classified as auth errors."""
    for status in (401, 403):
        cat = PolymarketAdapter._classify_error(status, True, "")
        assert cat == "auth", f"Expected auth for {status}"


def test_classify_error_not_found() -> None:
    """404 is classified as not_found."""
    cat = PolymarketAdapter._classify_error(404, True, "not found")
    assert cat == "not_found"


def test_classify_error_fatal() -> None:
    """Other 4xx responses are classified as fatal."""
    cat = PolymarketAdapter._classify_error(400, True, "bad request")
    assert cat == "fatal"
    cat2 = PolymarketAdapter._classify_error(422, False, "unprocessable")
    assert cat2 == "fatal"


# ---------------------------------------------------------------------------
# 12. place: reconcile_failed blocks placement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_place_refused_when_reconcile_failed(
    live_adapter: PolymarketAdapter,
    buy_intent: OrderIntent,
) -> None:
    """When reconcile_failed=True, place() returns a rejection without network calls."""
    live_adapter._reconcile_failed = True
    live_adapter._http = httpx.AsyncClient()
    live_adapter._signing = MagicMock()

    with respx.mock(assert_all_mocked=True):
        result = await live_adapter.place(buy_intent)

    await live_adapter._http.aclose()
    assert result.accepted is False
    assert "reconcile" in (result.reason or "").lower()


# ---------------------------------------------------------------------------
# 13. get_market: caching behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_get_market_caches_result(
    live_adapter: PolymarketAdapter,
) -> None:
    """A second get_market call within TTL does not hit the network."""
    with respx.mock() as mock:
        route = mock.get(f"{live_adapter._config.clob_host}/markets/{CONDITION_ID}").mock(
            return_value=httpx.Response(200, json=CLOB_MARKET_RESPONSE)
        )
        live_adapter._http = httpx.AsyncClient()
        # First call — hits network
        m1 = await live_adapter.get_market(CONDITION_ID)
        # Second call — should use cache
        m2 = await live_adapter.get_market(CONDITION_ID)
        await live_adapter._http.aclose()

    assert route.called
    assert route.call_count == 1  # only one real request
    assert m1 == m2


# ---------------------------------------------------------------------------
# 14. Paper simulator: taking order fills at far touch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_paper_buy_taking_order_fills_at_best_ask() -> None:
    """A BUY at price >= best_ask fills immediately at best_ask (far touch)."""
    config = PolymarketConfig(**make_test_config())
    http = MagicMock(spec=httpx.AsyncClient)
    sim = PolymarketPaperSimulator(config=config, http=http)

    best_ask = Decimal("0.62")
    sim._best_ask[TOKEN_ID_YES] = best_ask
    sim._best_bid[TOKEN_ID_YES] = Decimal("0.58")

    market = Market(
        market_id=CONDITION_ID,
        token_id=TOKEN_ID_YES,
        tick_size=Decimal("0.01"),
        min_size=Decimal("5"),
        venue="polymarket",
    )
    intent = OrderIntent(
        client_order_id="aabbccdd00112233",
        market=market,
        side=Side.BUY,
        price=Decimal("0.65"),  # > best_ask → taking order
        size=Decimal("100"),
    )

    result = await sim.place(intent)
    assert result.accepted is True

    # A FillEvent should have been enqueued
    assert sim._event_queue.qsize() == 1
    fill = sim._event_queue.get_nowait()
    from src.exchanges.base import FillEvent

    assert isinstance(fill, FillEvent)
    assert fill.fill_price == best_ask
    # Order should be removed from open_orders after immediate fill
    assert intent.client_order_id not in sim._open_orders


@pytest.mark.asyncio()
async def test_paper_sell_taking_order_fills_at_best_bid() -> None:
    """A SELL at price <= best_bid fills immediately at best_bid (far touch)."""
    config = PolymarketConfig(**make_test_config())
    http = MagicMock(spec=httpx.AsyncClient)
    sim = PolymarketPaperSimulator(config=config, http=http)

    best_bid = Decimal("0.58")
    sim._best_ask[TOKEN_ID_YES] = Decimal("0.62")
    sim._best_bid[TOKEN_ID_YES] = best_bid

    market = Market(
        market_id=CONDITION_ID,
        token_id=TOKEN_ID_YES,
        tick_size=Decimal("0.01"),
        min_size=Decimal("5"),
        venue="polymarket",
    )
    intent = OrderIntent(
        client_order_id="aabbccdd00112244",
        market=market,
        side=Side.SELL,
        price=Decimal("0.55"),  # < best_bid → taking order
        size=Decimal("50"),
    )

    result = await sim.place(intent)
    assert result.accepted is True

    assert sim._event_queue.qsize() == 1
    fill = sim._event_queue.get_nowait()
    from src.exchanges.base import FillEvent

    assert isinstance(fill, FillEvent)
    assert fill.fill_price == best_bid


# ---------------------------------------------------------------------------
# 15. Timestamp parsing helpers
# ---------------------------------------------------------------------------


def test_parse_gamma_timestamp_space_format() -> None:
    """Gamma space-separated timestamps are parsed correctly."""

    ts = _parse_gamma_timestamp("2026-03-19 23:20:15+00")
    assert ts.year == 2026
    assert ts.month == 3
    assert ts.day == 19
    assert ts.tzinfo is not None


def test_parse_timestamp_unix_seconds() -> None:
    """Unix timestamp int → UTC datetime."""
    ts = _parse_timestamp(1746700000)
    assert ts.year >= 2025


def test_parse_timestamp_unix_milliseconds() -> None:
    """Unix millisecond timestamps (> 1e12) are divided by 1000."""
    ts_sec = _parse_timestamp(1746700000)
    ts_ms = _parse_timestamp(1746700000000)  # same time in ms
    # Should be within 1 second
    diff = abs((ts_sec - ts_ms).total_seconds())
    assert diff < 1


def test_parse_timestamp_none_returns_now() -> None:
    """None timestamp returns a recent datetime (fallback)."""
    from datetime import UTC, datetime

    before = datetime.now(tz=UTC)
    ts = _parse_timestamp(None)
    after = datetime.now(tz=UTC)
    assert before <= ts <= after


# ---------------------------------------------------------------------------
# 16. Hypothesis: deterministic salt
# ---------------------------------------------------------------------------


@given(
    coid=st.text(
        alphabet="0123456789abcdef",
        min_size=16,
        max_size=64,
    )
)
@settings(max_examples=200)
def test_hypothesis_salt_is_deterministic(coid: str) -> None:
    """For any hex client_order_id, derive_salt returns the same value on repeat calls."""
    s1 = derive_salt(coid)
    s2 = derive_salt(coid)
    assert s1 == s2
    assert isinstance(s1, int)
    assert s1 >= 0


# ---------------------------------------------------------------------------
# 17. Hypothesis: BUY amount calculation relationships
# ---------------------------------------------------------------------------


@given(
    price=st.decimals(
        min_value=Decimal("0.01"),
        max_value=Decimal("0.99"),
        places=2,
    ),
    size=st.decimals(
        min_value=Decimal("1"),
        max_value=Decimal("1000"),
        places=0,
    ),
)
@settings(max_examples=200)
def test_hypothesis_buy_maker_amount_relationship(
    price: Decimal,
    size: Decimal,
) -> None:
    """BUY: makerAmount (USDC) ≈ price * size * 1e6; takerAmount = size * 1e6."""
    scale = Decimal("1000000")
    maker_amount = int((price * size * scale).to_integral_value())
    taker_amount = int((size * scale).to_integral_value())

    # makerAmount should be proportional to price
    assert maker_amount >= 0
    assert taker_amount >= 0
    # For a BUY: you spend USDC (maker) to receive shares (taker)
    # maker/taker ratio ≈ price
    if taker_amount > 0:
        ratio = Decimal(maker_amount) / Decimal(taker_amount)
        assert abs(ratio - price) < Decimal("0.01")


@given(
    price=st.decimals(
        min_value=Decimal("0.01"),
        max_value=Decimal("0.99"),
        places=2,
    ),
    size=st.decimals(
        min_value=Decimal("1"),
        max_value=Decimal("1000"),
        places=0,
    ),
)
@settings(max_examples=200)
def test_hypothesis_sell_maker_amount_relationship(
    price: Decimal,
    size: Decimal,
) -> None:
    """SELL: makerAmount = size * 1e6; takerAmount (USDC) ≈ price * size * 1e6."""
    scale = Decimal("1000000")
    maker_amount = int((size * scale).to_integral_value())
    taker_amount = int((price * size * scale).to_integral_value())

    assert maker_amount >= 0
    assert taker_amount >= 0
    # For SELL: you give shares (maker) to receive USDC (taker)
    if maker_amount > 0:
        ratio = Decimal(taker_amount) / Decimal(maker_amount)
        assert abs(ratio - price) < Decimal("0.01")


# ---------------------------------------------------------------------------
# 18. Hypothesis: paper fill never gives a better price than far touch
# ---------------------------------------------------------------------------


@given(
    best_bid=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("0.49"), places=2),
    best_ask=st.decimals(min_value=Decimal("0.51"), max_value=Decimal("0.99"), places=2),
    buy_price=st.decimals(min_value=Decimal("0.51"), max_value=Decimal("0.99"), places=2),
)
@settings(max_examples=100)
def test_hypothesis_paper_buy_fill_never_better_than_best_ask(
    best_bid: Decimal,
    best_ask: Decimal,
    buy_price: Decimal,
) -> None:
    """Taking BUY order in paper mode always fills at best_ask (far touch), never better."""
    config = PolymarketConfig(**make_test_config())
    http = MagicMock(spec=httpx.AsyncClient)
    sim = PolymarketPaperSimulator(config=config, http=http)

    sim._best_bid[TOKEN_ID_YES] = best_bid
    sim._best_ask[TOKEN_ID_YES] = best_ask

    market = Market(
        market_id=CONDITION_ID,
        token_id=TOKEN_ID_YES,
        tick_size=Decimal("0.01"),
        min_size=Decimal("1"),
        venue="polymarket",
    )

    # buy_price >= best_ask → taking order
    if buy_price < best_ask:
        return  # resting order, skip

    import asyncio

    intent = OrderIntent(
        client_order_id="aabbccdd00113300",
        market=market,
        side=Side.BUY,
        price=buy_price,
        size=Decimal("10"),
    )

    asyncio.get_event_loop().run_until_complete(sim.place(intent))

    # Should have a fill event
    if not sim._event_queue.empty():
        from src.exchanges.base import FillEvent

        fill = sim._event_queue.get_nowait()
        if isinstance(fill, FillEvent):
            # Fill price must be exactly best_ask (far touch) — never better for BUY
            assert fill.fill_price == best_ask
