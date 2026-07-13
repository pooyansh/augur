"""Unit tests for the Tier 1 Gamma ``/events/keyset`` collector.

All HTTP is mocked via ``respx`` — no real network calls in this suite.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import respx

from ml.data_collection.events import (
    build_manifest,
    collect_events,
    events_output_dir,
    write_events_parquet,
    write_schema_doc,
)

_GAMMA_URL = "https://gamma-api.polymarket.com/events/keyset"

_BASE_CREATED = datetime(2025, 1, 1, tzinfo=UTC)
_BASE_START = datetime(2025, 6, 1, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_event(
    index: int,
    *,
    series_id: str,
    series_slug: str,
    market_title: str = "Test Recurring Series",
    missing_final_price: bool = False,
) -> dict[str, Any]:
    """Build one synthetic Gamma event dict shaped like a real API response."""
    created = _BASE_CREATED + timedelta(seconds=index)
    start = _BASE_START + timedelta(minutes=5 * index)
    end = start + timedelta(minutes=5)
    slug = f"{series_slug}-{int(start.timestamp())}"

    event_metadata: dict[str, float] = {"priceToBeat": 100.0 + index}
    if not missing_final_price:
        event_metadata["finalPrice"] = 100.5 + index

    outcome_prices = ["1", "0"] if index % 2 == 0 else ["0", "1"]

    return {
        "id": str(1000 + index),
        "slug": slug,
        "title": f"Test window {index}",
        "startDate": _iso(created),
        "startTime": _iso(start),
        "endDate": _iso(end),
        "series": [{"id": series_id, "slug": series_slug, "title": market_title}],
        "markets": [
            {
                "conditionId": f"0xcond{index}",
                "outcomes": json.dumps(["Up", "Down"]),
                "clobTokenIds": json.dumps([f"tok-up-{index}", f"tok-down-{index}"]),
                "outcomePrices": json.dumps(outcome_prices),
                "volume": str(Decimal("123.456") + index),
            }
        ],
        "eventMetadata": event_metadata,
    }


def make_fake_gamma_keyset_handler(
    events_by_series: dict[str, list[dict[str, Any]]],
    *,
    server_max_page_size: int = 100,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a respx side_effect simulating Gamma's real ``/events/keyset`` behavior.

    Honors ``series_id``, ``closed``, ``start_date_min``, ``limit`` (silently
    clamped to ``server_max_page_size``, mirroring the real API — a requested
    ``limit`` above the server's cap is *not* rejected, it's just truncated,
    which means a page shorter than the *requested* limit is NOT a valid
    end-of-results signal), and ``after_cursor`` (an opaque cursor — here just
    an index into the filtered/sorted pool, since the mock only needs to
    round-trip it faithfully, not match the real opaque encoding).

    Termination is only ever signalled by an empty ``events`` list, exactly
    like production.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        series_id = params.get("series_id", "")
        start_date_min = params.get("start_date_min", "")
        requested_limit = int(params.get("limit", 100))
        effective_limit = min(requested_limit, server_max_page_size)
        after_cursor = params.get("after_cursor")
        start_index = int(after_cursor) if after_cursor else 0

        pool = events_by_series.get(series_id, [])
        filtered = sorted(
            (e for e in pool if e["startDate"] >= start_date_min),
            key=lambda e: (e["startDate"], e["slug"]),
        )
        page = filtered[start_index : start_index + effective_limit]
        next_index = start_index + len(page)
        return httpx.Response(
            200,
            json={
                "$schema": "https://gamma-api.polymarket.com/schemas/EventsKeysetListResponse.json",
                "events": page,
                "next_cursor": str(next_index),
            },
        )

    return _handler


@respx.mock
def test_pagination_advances_via_after_cursor_without_duplicates_or_gaps() -> None:
    series_id = "999"
    series_slug = "fake-series-a"
    total_windows = 260
    events = [
        make_event(i, series_id=series_id, series_slug=series_slug) for i in range(total_windows)
    ]
    # Server clamps pages to 50 even though we request page_limit=200 below —
    # this proves collection doesn't stop early on a "short" (50 < 200) page.
    handler = make_fake_gamma_keyset_handler({series_id: events}, server_max_page_size=50)
    respx.get(_GAMMA_URL).mock(side_effect=handler)

    df = collect_events(series_id, venue="polymarket", closed=True, page_limit=200)

    assert df.height == total_windows
    assert df["window_slug"].n_unique() == total_windows
    start_ts = df["start_ts"].to_list()
    assert start_ts == sorted(start_ts)


@respx.mock
def test_missing_final_price_does_not_crash_collection() -> None:
    series_id = "888"
    series_slug = "fake-series-b"
    events = [
        make_event(0, series_id=series_id, series_slug=series_slug),
        make_event(1, series_id=series_id, series_slug=series_slug, missing_final_price=True),
        make_event(2, series_id=series_id, series_slug=series_slug),
    ]
    handler = make_fake_gamma_keyset_handler({series_id: events})
    respx.get(_GAMMA_URL).mock(side_effect=handler)

    df = collect_events(series_id, page_limit=100)

    assert df.height == 3
    final_prices = df.sort("start_ts")["final_price"].to_list()
    assert final_prices[1] is None
    assert final_prices[0] is not None
    assert final_prices[2] is not None
    # priceToBeat was present on all three rows — missing finalPrice alone
    # must not blank out sibling fields.
    assert df["price_to_beat"].null_count() == 0


@respx.mock
def test_manifest_sha256_is_stable_for_identical_inputs() -> None:
    series_id = "777"
    series_slug = "fake-series-c"
    events = [make_event(i, series_id=series_id, series_slug=series_slug) for i in range(5)]
    handler = make_fake_gamma_keyset_handler({series_id: events})
    respx.get(_GAMMA_URL).mock(side_effect=handler)

    df = collect_events(series_id, page_limit=100)

    manifest_a = build_manifest(
        df,
        venue="polymarket",
        series_id=series_id,
        closed=True,
        start_date_min="2020-01-01T00:00:00Z",
    )
    manifest_b = build_manifest(
        df,
        venue="polymarket",
        series_id=series_id,
        closed=True,
        start_date_min="2020-01-01T00:00:00Z",
    )

    assert manifest_a.params_sha256 == manifest_b.params_sha256
    # collected_at is expected to differ between calls; it must not affect the hash.
    assert manifest_a.row_count == 5

    manifest_different_params = build_manifest(
        df,
        venue="polymarket",
        series_id=series_id,
        closed=False,
        start_date_min="2020-01-01T00:00:00Z",
    )
    assert manifest_different_params.params_sha256 != manifest_a.params_sha256


@respx.mock
def test_money_fields_round_trip_through_parquet_as_strings(tmp_path: Path) -> None:
    series_id = "666"
    series_slug = "fake-series-d"
    events = [make_event(i, series_id=series_id, series_slug=series_slug) for i in range(3)]
    handler = make_fake_gamma_keyset_handler({series_id: events})
    respx.get(_GAMMA_URL).mock(side_effect=handler)

    df = collect_events(series_id, page_limit=100)
    out_path = write_events_parquet(df, root=tmp_path, venue="polymarket", series_slug=series_slug)

    reloaded = pl.read_parquet(out_path)
    for column in ("price_to_beat", "final_price", "lifetime_volume"):
        assert reloaded.schema[column] == pl.Utf8
        for value in reloaded[column].drop_nulls().to_list():
            Decimal(value)  # must not raise — round-trips exactly as text


@respx.mock
def test_output_path_partitioned_by_venue_and_series_no_collision(tmp_path: Path) -> None:
    series_id_a, series_slug_a = "111", "fake-series-e"
    series_id_b, series_slug_b = "222", "fake-series-f"
    events_a = [make_event(i, series_id=series_id_a, series_slug=series_slug_a) for i in range(2)]
    events_b = [make_event(i, series_id=series_id_b, series_slug=series_slug_b) for i in range(2)]
    handler = make_fake_gamma_keyset_handler({series_id_a: events_a, series_id_b: events_b})
    respx.get(_GAMMA_URL).mock(side_effect=handler)

    df_a = collect_events(series_id_a, page_limit=100)
    df_b = collect_events(series_id_b, page_limit=100)

    write_schema_doc(tmp_path, "polymarket", series_slug_a)
    write_schema_doc(tmp_path, "polymarket", series_slug_b)
    path_a = write_events_parquet(
        df_a, root=tmp_path, venue="polymarket", series_slug=series_slug_a
    )
    path_b = write_events_parquet(
        df_b, root=tmp_path, venue="polymarket", series_slug=series_slug_b
    )

    assert path_a != path_b
    assert path_a.exists()
    assert path_b.exists()
    expected_dir_a = events_output_dir(tmp_path, "polymarket", series_slug_a)
    expected_dir_b = events_output_dir(tmp_path, "polymarket", series_slug_b)
    assert str(path_a).startswith(str(expected_dir_a))
    assert str(path_b).startswith(str(expected_dir_b))
    assert expected_dir_a != expected_dir_b
    # Each series' own SCHEMA.md is independent — no collision.
    assert (expected_dir_a / "SCHEMA.md").exists()
    assert (expected_dir_b / "SCHEMA.md").exists()
