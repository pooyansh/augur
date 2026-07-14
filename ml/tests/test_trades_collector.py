"""Unit tests for the Tier 2 per-second trades collector.

All HTTP is mocked via ``respx`` — no real network calls in this suite.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import respx

from ml.data_collection.trades import (
    _collect_window_trades_impl,
    build_manifest,
    collect_window_trades,
    collect_windows,
    load_checkpoint,
    seconds_output_dir,
    write_window_trades_parquet,
)

_TRADES_URL = "https://data-api.polymarket.com/trades"

_CONDITION_ID = "0xcond-a"
_TOKEN_UP = "tok-up-a"
_TOKEN_DOWN = "tok-down-a"
_WINDOW_SLUG = "btc-updown-5m-1771459500"
_START_TS = datetime(2026, 2, 19, 0, 5, 0, tzinfo=UTC)
_START_EPOCH = int(_START_TS.timestamp())


def make_trade(
    *, asset: str, price: float, size: float, second_offset: int, outcome: str = "Up"
) -> dict[str, Any]:
    """Build one synthetic trade dict shaped like a real Data API response."""
    return {
        "asset": asset,
        "conditionId": _CONDITION_ID,
        "price": price,
        "size": size,
        "side": "BUY",
        "timestamp": _START_EPOCH + second_offset,
        "outcome": outcome,
        "slug": _WINDOW_SLUG,
    }


def make_trades_handler(
    trades_by_market: dict[str, list[dict[str, Any]]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a respx side_effect simulating full offset/limit pagination."""

    def _handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        market = params.get("market", "")
        limit = int(params.get("limit", 500))
        offset = int(params.get("offset", 0))
        pool = trades_by_market.get(market, [])
        page = pool[offset : offset + limit]
        return httpx.Response(200, json=page)

    return _handler


def make_offset_ceiling_handler(
    trades_by_market: dict[str, list[dict[str, Any]]], *, max_offset: int
) -> Callable[[httpx.Request], httpx.Response]:
    """Simulate the Data API's real (undocumented) offset ceiling behavior."""

    def _handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        market = params.get("market", "")
        limit = int(params.get("limit", 500))
        offset = int(params.get("offset", 0))
        if offset > max_offset:
            return httpx.Response(
                400, json={"error": f"max historical activity offset of {max_offset} exceeded"}
            )
        pool = trades_by_market.get(market, [])
        page = pool[offset : offset + limit]
        return httpx.Response(200, json=page)

    return _handler


@respx.mock
def test_pagination_stops_on_short_page_no_duplicates() -> None:
    total_trades = 1250  # 2 full pages of 500 + 1 short page of 250
    trades = [
        make_trade(asset=_TOKEN_UP, price=0.5, size=1.0, second_offset=i % 300)
        for i in range(total_trades)
    ]
    handler = make_trades_handler({_CONDITION_ID: trades})
    route = respx.get(_TRADES_URL).mock(side_effect=handler)

    df = collect_window_trades(
        _CONDITION_ID, _TOKEN_UP, _TOKEN_DOWN, _WINDOW_SLUG, _START_TS, outcome="UP"
    )

    assert route.call_count == 3  # 500 + 500 + 250(short) -> stop
    assert df.height == 2 * 300
    total_trade_count = df.filter(pl.col("token_id") == _TOKEN_UP)["trade_count"].sum()
    assert total_trade_count == total_trades


@respx.mock
def test_every_second_present_with_sparse_trades() -> None:
    # Only 4 trades scattered across a 300-second window.
    trades = [
        make_trade(asset=_TOKEN_UP, price=0.40, size=10.0, second_offset=5),
        make_trade(asset=_TOKEN_UP, price=0.55, size=3.0, second_offset=100),
        make_trade(asset=_TOKEN_DOWN, price=0.60, size=2.0, second_offset=50, outcome="Down"),
        make_trade(asset=_TOKEN_DOWN, price=0.01, size=7.0, second_offset=299, outcome="Down"),
    ]
    handler = make_trades_handler({_CONDITION_ID: trades})
    respx.get(_TRADES_URL).mock(side_effect=handler)

    df = collect_window_trades(
        _CONDITION_ID, _TOKEN_UP, _TOKEN_DOWN, _WINDOW_SLUG, _START_TS, outcome="DOWN"
    )

    up = df.filter(pl.col("token_id") == _TOKEN_UP).sort("second_offset")
    down = df.filter(pl.col("token_id") == _TOKEN_DOWN).sort("second_offset")
    assert up["second_offset"].to_list() == list(range(300))
    assert down["second_offset"].to_list() == list(range(300))
    # outcome is denormalized onto every row of the window, same value.
    assert set(df["outcome"].to_list()) == {"DOWN"}


@respx.mock
def test_forward_fill_and_leading_null_correctness() -> None:
    trades = [
        make_trade(asset=_TOKEN_UP, price=0.40, size=10.0, second_offset=5),
        make_trade(asset=_TOKEN_UP, price=0.55, size=3.0, second_offset=100),
    ]
    handler = make_trades_handler({_CONDITION_ID: trades})
    respx.get(_TRADES_URL).mock(side_effect=handler)

    df = collect_window_trades(_CONDITION_ID, _TOKEN_UP, _TOKEN_DOWN, _WINDOW_SLUG, _START_TS)
    up = df.filter(pl.col("token_id") == _TOKEN_UP).sort("second_offset")
    prices = up["price"].to_list()

    # Leading seconds (before the first trade at offset=5) are null, not fabricated.
    assert prices[0:5] == [None] * 5
    # Second 5 itself, and every second up to (not including) the next trade
    # at 100, is forward-filled at 0.40.
    assert prices[5] == "0.4"
    assert prices[6:100] == ["0.4"] * 94
    # From second 100 onward, forward-filled at the new price.
    assert prices[100] == "0.55"
    assert prices[299] == "0.55"


@respx.mock
def test_volume_and_trade_count_zero_not_null_when_no_trade() -> None:
    trades = [make_trade(asset=_TOKEN_UP, price=0.40, size=10.0, second_offset=5)]
    handler = make_trades_handler({_CONDITION_ID: trades})
    respx.get(_TRADES_URL).mock(side_effect=handler)

    df = collect_window_trades(_CONDITION_ID, _TOKEN_UP, _TOKEN_DOWN, _WINDOW_SLUG, _START_TS)
    up = df.filter(pl.col("token_id") == _TOKEN_UP).sort("second_offset")

    assert up["volume"].null_count() == 0
    assert up["trade_count"].null_count() == 0
    no_trade_row = up.filter(pl.col("second_offset") == 6)
    assert no_trade_row["volume"].item() == "0"
    assert no_trade_row["trade_count"].item() == 0
    trade_row = up.filter(pl.col("second_offset") == 5)
    assert trade_row["volume"].item() == "10.0"
    assert trade_row["trade_count"].item() == 1


@respx.mock
def test_checkpoint_resume_skips_already_done_windows(tmp_path: Path) -> None:
    windows = [
        {
            "window_slug": "w1",
            "condition_id": "0xcond-1",
            "token_id_up": "tok-up-1",
            "token_id_down": "tok-down-1",
            "start_ts": _START_TS,
            "market_name": "Test Series",
            "window_name": "Window 1",
            "outcome": "UP",
        },
        {
            "window_slug": "w2",
            "condition_id": "0xcond-2",
            "token_id_up": "tok-up-2",
            "token_id_down": "tok-down-2",
            "start_ts": _START_TS,
            "market_name": "Test Series",
            "window_name": "Window 2",
            "outcome": "DOWN",
        },
    ]
    events_df = pl.DataFrame(windows)

    checkpoint_path = tmp_path / "checkpoint.jsonl"
    checkpoint_path.write_text(
        json.dumps(
            {
                "window_slug": "w1",
                "row_count": 600,
                "start_ts": _START_TS.isoformat(),
                "completed_at": _START_TS.isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Only w2's market should ever be requested.
    trades_w2 = [make_trade(asset="tok-up-2", price=0.5, size=1.0, second_offset=10)]
    handler = make_trades_handler({"0xcond-2": trades_w2})
    route = respx.get(_TRADES_URL).mock(side_effect=handler)

    summary = collect_windows(events_df, output_root=tmp_path, checkpoint_path=checkpoint_path)

    assert summary.windows_skipped_already_done == 1
    assert summary.windows_processed_this_run == 1
    # Every request that landed must have been for w2's market only.
    for call in route.calls:
        assert dict(httpx.Request(call.request.method, call.request.url).url.params)["market"] == (
            "0xcond-2"
        )

    done = load_checkpoint(checkpoint_path)
    assert done == {"w1", "w2"}
    written = seconds_output_dir(tmp_path, "polymarket", "btc-up-or-down-5m")
    assert list(written.rglob("seconds-w2.parquet"))
    assert not list(written.rglob("seconds-w1.parquet"))  # w1 was never (re)written


@respx.mock
def test_decimal_fields_round_trip_through_parquet_as_strings(tmp_path: Path) -> None:
    trades = [
        make_trade(asset=_TOKEN_UP, price=0.333, size=12.75, second_offset=1),
        make_trade(asset=_TOKEN_DOWN, price=0.667, size=4.5, second_offset=2, outcome="Down"),
    ]
    handler = make_trades_handler({_CONDITION_ID: trades})
    respx.get(_TRADES_URL).mock(side_effect=handler)

    df = collect_window_trades(
        _CONDITION_ID, _TOKEN_UP, _TOKEN_DOWN, _WINDOW_SLUG, _START_TS, window_seconds=10
    )
    out_path = write_window_trades_parquet(
        df,
        root=tmp_path,
        venue="polymarket",
        series_slug="btc-up-or-down-5m",
        window_slug=_WINDOW_SLUG,
        window_date=_START_TS.date(),
    )

    reloaded = pl.read_parquet(out_path)
    for column in ("price", "volume"):
        assert reloaded.schema[column] == pl.Utf8
        for value in reloaded[column].drop_nulls().to_list():
            Decimal(value)  # must not raise — round-trips exactly as text


@respx.mock
def test_manifest_reproducible_from_checkpoint(tmp_path: Path) -> None:
    trades = [make_trade(asset=_TOKEN_UP, price=0.5, size=1.0, second_offset=1)]
    handler = make_trades_handler({_CONDITION_ID: trades})
    respx.get(_TRADES_URL).mock(side_effect=handler)

    events_df = pl.DataFrame(
        [
            {
                "window_slug": _WINDOW_SLUG,
                "condition_id": _CONDITION_ID,
                "token_id_up": _TOKEN_UP,
                "token_id_down": _TOKEN_DOWN,
                "start_ts": _START_TS,
                "market_name": "Test Series",
                "window_name": "Window",
                "outcome": "UP",
            }
        ]
    )
    checkpoint_path = tmp_path / "checkpoint.jsonl"
    summary = collect_windows(
        events_df, output_root=tmp_path, checkpoint_path=checkpoint_path, window_seconds=10
    )

    manifest_a = build_manifest(
        checkpoint_path,
        venue="polymarket",
        series_slug="btc-up-or-down-5m",
        window_count_total_this_run=summary.windows_total,
    )
    manifest_b = build_manifest(
        checkpoint_path,
        venue="polymarket",
        series_slug="btc-up-or-down-5m",
        window_count_total_this_run=summary.windows_total,
    )

    assert manifest_a.params_sha256 == manifest_b.params_sha256
    assert manifest_a.window_count_completed == 1
    assert manifest_a.row_count_completed == 20  # 2 tokens * 10 seconds


@respx.mock
def test_offset_ceiling_stops_pagination_without_raising_and_flags_truncation(
    tmp_path: Path,
) -> None:
    # Simulate the Data API's real, undocumented offset ceiling (verified
    # empirically against the live API on 2026-07-12): requests with
    # offset > max_offset return HTTP 400, not a short/empty page. The
    # collector must treat this as "stop pagination, flag truncated" rather
    # than crash.
    trades = [
        make_trade(asset=_TOKEN_UP, price=0.5, size=1.0, second_offset=i % 300)
        for i in range(700)  # more than max_offset(500) + one page(500) can retrieve
    ]
    handler = make_offset_ceiling_handler({_CONDITION_ID: trades}, max_offset=300)
    respx.get(_TRADES_URL).mock(side_effect=handler)

    df, truncated = _collect_window_trades_impl(
        _CONDITION_ID, _TOKEN_UP, _TOKEN_DOWN, _WINDOW_SLUG, _START_TS, page_limit=500
    )

    assert truncated is True
    assert df.height == 2 * 300  # still every second present despite truncation

    events_df = pl.DataFrame(
        [
            {
                "window_slug": _WINDOW_SLUG,
                "condition_id": _CONDITION_ID,
                "token_id_up": _TOKEN_UP,
                "token_id_down": _TOKEN_DOWN,
                "start_ts": _START_TS,
                "market_name": "Test Series",
                "window_name": "Window",
                "outcome": "UP",
            }
        ]
    )
    checkpoint_path = tmp_path / "checkpoint.jsonl"
    summary = collect_windows(
        events_df, output_root=tmp_path, checkpoint_path=checkpoint_path, page_limit=500
    )
    assert summary.windows_truncated_this_run == 1
    records = [json.loads(line) for line in checkpoint_path.read_text().splitlines()]
    assert records[0]["truncated"] is True
