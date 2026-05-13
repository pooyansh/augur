"""Captured payloads for Polymarket adapter tests.

All responses are representative of real Polymarket API shapes.
No network calls are made — these are static fixtures for unit tests.

Shapes documented:
  - WS initial snapshot (list shape, no event_type)
  - WS price_change update (object shape, has event_type)
  - CLOB GET /markets/{condition_id} response
  - Gamma GET /markets/{condition_id} active market response
  - Gamma GET /markets/{condition_id} resolved market response
  - CLOB POST /order accepted response
  - Error responses: JSON 404, plain-text 404, 401
  - CLOB GET /data/orders orderbook response
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# Stable test identifiers
# ---------------------------------------------------------------------------

CONDITION_ID = "0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af"
TOKEN_ID_YES = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
TOKEN_ID_NO = "52114319501245915516055106046884209969926127482827954674443846427813813222426"

WALLET_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
EXCHANGE_ORDER_ID = "0xabc123def456abc123def456abc123def456abc123def456abc123def456abc12"

# ---------------------------------------------------------------------------
# WS: initial snapshot (list shape — no event_type field)
# ---------------------------------------------------------------------------

WS_SNAPSHOT_LIST: list[dict[str, Any]] = [
    {
        "asset_id": TOKEN_ID_YES,
        "hash": "0x1234",
        "market": CONDITION_ID,
        "bids": [
            {"price": "0.58", "size": "100"},
            {"price": "0.55", "size": "200"},
        ],
        "asks": [
            {"price": "0.62", "size": "150"},
            {"price": "0.65", "size": "300"},
        ],
        "timestamp": "1746700000",
    },
    {
        "asset_id": TOKEN_ID_NO,
        "hash": "0x5678",
        "market": CONDITION_ID,
        "bids": [
            {"price": "0.38", "size": "100"},
        ],
        "asks": [
            {"price": "0.42", "size": "150"},
        ],
        "timestamp": "1746700000",
    },
]

# ---------------------------------------------------------------------------
# WS: price_change update (object shape — has event_type)
# ---------------------------------------------------------------------------

WS_PRICE_CHANGE_UPDATE: dict[str, Any] = {
    "event_type": "price_change",
    "asset_id": TOKEN_ID_YES,
    "market": CONDITION_ID,
    "bids": [
        {"price": "0.60", "size": "150"},
    ],
    "asks": [
        {"price": "0.63", "size": "120"},
    ],
    "timestamp": "1746700060",
    "hash": "0xabcd",
}

# ---------------------------------------------------------------------------
# WS: trade / fill event
# ---------------------------------------------------------------------------

WS_TRADE_UPDATE: dict[str, Any] = {
    "event_type": "last_trade_price",
    "asset_id": TOKEN_ID_YES,
    "market": CONDITION_ID,
    "id": EXCHANGE_ORDER_ID,
    "maker_order_id": EXCHANGE_ORDER_ID,
    "price": "0.62",
    "size": "50",
    "fee": "0.0155",
    "timestamp": "1746700120",
}

# ---------------------------------------------------------------------------
# CLOB: GET /markets/{condition_id}
# ---------------------------------------------------------------------------

CLOB_MARKET_RESPONSE: dict[str, Any] = {
    "condition_id": CONDITION_ID,
    "question_id": "0xqid123",
    "question": "Will BTC reach $100,000 by end of 2026?",
    "minimum_tick_size": "0.01",
    "minimum_order_size": "5",
    "active": True,
    "closed": False,
    "accepting_orders": True,
    "tokens": [
        {
            "token_id": TOKEN_ID_YES,
            "outcome": "Yes",
            "winner": False,
        },
        {
            "token_id": TOKEN_ID_NO,
            "outcome": "No",
            "winner": False,
        },
    ],
    "rewards": {
        "max_spread": "0.02",
        "event_start_date": None,
    },
}

# ---------------------------------------------------------------------------
# Gamma: active market response
# ---------------------------------------------------------------------------

GAMMA_MARKET_ACTIVE: dict[str, Any] = {
    "id": CONDITION_ID,
    "slug": "btc-100k-2026",
    "question": "Will BTC reach $100,000 by end of 2026?",
    "closed": False,
    "active": True,
    "acceptingOrders": True,
    "clobTokenIds": f'["{TOKEN_ID_YES}", "{TOKEN_ID_NO}"]',
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.62", "0.38"]',
    "umaResolutionStatuses": '["proposed", "proposed"]',
    "updatedAt": "2026-05-01 12:00:00+00",
    "closedTime": None,
    "volume": "50000.00",
    "liquidity": "10000.00",
}

# ---------------------------------------------------------------------------
# Gamma: resolved market response
# ---------------------------------------------------------------------------

GAMMA_MARKET_RESOLVED: dict[str, Any] = {
    "id": CONDITION_ID,
    "slug": "btc-100k-2026",
    "question": "Will BTC reach $100,000 by end of 2026?",
    "closed": True,
    "active": False,
    "acceptingOrders": False,
    "clobTokenIds": f'["{TOKEN_ID_YES}", "{TOKEN_ID_NO}"]',
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["1.0", "0.0"]',
    "umaResolutionStatuses": '["resolved", "resolved"]',
    "updatedAt": "2026-12-31 23:59:59+00",
    "closedTime": "2026-12-31 23:20:15+00",
    "volume": "250000.00",
    "liquidity": "0.00",
}

# ---------------------------------------------------------------------------
# CLOB: POST /order accepted response
# ---------------------------------------------------------------------------

CLOB_ORDER_ACCEPTED: dict[str, Any] = {
    "success": True,
    "errorMsg": "",
    "orderID": EXCHANGE_ORDER_ID,
    "transactionsHashes": [],
    "status": "matched",
}

# ---------------------------------------------------------------------------
# CLOB: POST /order rejected (JSON)
# ---------------------------------------------------------------------------

CLOB_ORDER_REJECTED_JSON: dict[str, Any] = {
    "success": False,
    "errorMsg": "Order size below minimum",
    "error": "Order size below minimum",
    "orderID": None,
}

# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------

# JSON 404 (e.g. market not found)
ERROR_JSON_404: dict[str, Any] = {
    "error": "not found",
    "message": "Market not found",
}

# Plain-text 404 (e.g. nginx-style)
ERROR_PLAINTEXT_404 = "404 page not found\n"

# 401 Unauthorized (JSON)
ERROR_JSON_401: dict[str, Any] = {
    "error": "Unauthorized",
    "message": "Invalid API key or signature",
}

# 429 Rate Limited
ERROR_JSON_429: dict[str, Any] = {
    "error": "Too Many Requests",
    "message": "Rate limit exceeded",
}

# ---------------------------------------------------------------------------
# CLOB: GET /data/orders (open orders)
# ---------------------------------------------------------------------------

CLOB_OPEN_ORDERS: list[dict[str, Any]] = [
    {
        "id": EXCHANGE_ORDER_ID,
        "asset_id": TOKEN_ID_YES,
        "market": CONDITION_ID,
        "side": "BUY",
        "price": "0.60",
        "original_size": "100",
        "size_filled": "0",
        "size_matched": "0",
        "outcome": "Yes",
        "making": False,
        "status": "LIVE",
        "owner": WALLET_ADDRESS,
        "created_at": 1746700000,
    }
]

# ---------------------------------------------------------------------------
# Helper: build a PolymarketConfig for tests (no real secrets needed)
# ---------------------------------------------------------------------------

_TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def make_test_config() -> dict[str, Any]:
    """Return a dict suitable for constructing PolymarketConfig in tests.

    Uses Hardhat account #0 private key — never use on mainnet.

    Returns:
        Dict of all PolymarketConfig fields.
    """
    return {
        "l1_private_key": _TEST_PRIVATE_KEY,
        "l2_api_key": "test-api-key",
        "l2_secret": "dGVzdC1zZWNyZXQ=",  # base64("test-secret")
        "l2_passphrase": "test-passphrase",
        "wallet_address": WALLET_ADDRESS,
        "signature_type": 0,
        "max_allowance_usdc": Decimal("10000"),
        "balance_ceiling_usdc": Decimal("5000"),
        "allowance_cap_usdc": Decimal("10000"),
        "clob_host": "https://clob.polymarket.com",
        "gamma_host": "https://gamma-api.polymarket.com",
        "ws_host": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    }
