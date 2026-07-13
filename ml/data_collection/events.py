"""Tier 1 collector — Gamma ``/events`` for a recurring-market series.

Pulls every closed event (settlement window) for a given Polymarket Gamma
series id and returns one row per window as a Polars DataFrame. Generalizes
across any Gamma recurring series (different assets, different cadences) —
``series_id`` and ``venue`` are always explicit parameters, never module-level
constants baked into request logic. ``series_id="10684"`` /
``series_slug="btc-up-or-down-5m"`` (BTC Up or Down 5m) is only the example
this module is exercised against; nothing in the collection or pagination
logic below is BTC-specific.

Pagination note — read before touching this file
--------------------------------------------------
Gamma exposes two pagination mechanisms for events:

* ``offset``/``limit`` on the plain ``/events`` endpoint — works, but Gamma
  refuses requests past a server-side offset ceiling (observed ~2000 for a
  filtered ``series_id``+``closed`` query as of 2026-07-13) with HTTP 422:
  ``{"error": "offset too large, use /events/keyset for deeper pagination"}``.
* cursor-based pagination on ``/events/keyset`` — per Gamma's own error
  message this is the documented answer to the offset ceiling. **It does
  not work as documented, verified empirically against the live prod API
  on 2026-07-13:**
    - Resending the same filters (``series_id``, ``closed``, ``order``,
      ``ascending``) plus ``cursor=<next_cursor>`` returns the *identical
      first page* again, repeatably, across many trials — the returned
      ``next_cursor`` value doesn't even change between calls, meaning the
      server is not incorporating the inbound cursor into its query at all
      whenever query filters are present.
    - Dropping the filters and sending only ``cursor`` + ``limit`` doesn't
      fix it either: it silently drops the filter context and returns
      whatever the *global* (all-series) keyset page 1 is — completely
      unrelated markets (NBA/NFL), not ``series_id=10684`` continued. That
      "page" is also static across repeated calls with the same cursor.
  In short: under every combination tried, the inbound ``cursor`` value is
  never actually consumed/advanced by the server. This looks like a
  server-side bug in the deployed Gamma API, not a client usage error — but
  we can't rule out an undocumented required shape (e.g. a POST body) we
  didn't think to try. Given the working fallback below, we didn't sink
  further time into it.

Working approach — date-fenced offset pagination
--------------------------------------------------
Plain ``/events`` supports a ``start_date_min`` filter (event ``startDate``
>= the given ISO timestamp; ``startDate`` is Gamma's internal
record-creation-ish timestamp, monotonically close to but not identical to
the window's actual trading start — it is only used here as an ordering/
fencing key, never stored as ``start_ts``). We page normally with
offset/limit, sorted ascending by ``startDate``, until the API returns the
422 "offset too large" error. At that point we take the maximum
``startDate`` seen so far, set ``start_date_min`` to that value, reset
``offset`` back to ``0``, and resume ("advance the fence"). This never
depends on knowing the exact offset ceiling — we just react to the 422 —
and it terminates when a page comes back shorter than the requested limit
(the true end of the result set). Rows that straddle a fence boundary
(events can share the same millisecond-precision ``startDate``) are
deduplicated by ``window_slug`` before being yielded.

If a future Gamma deploy fixes ``/events/keyset`` cursor pagination, it
could replace this fencing scheme — re-verify with the same black-box test
described above before relying on it again.
"""

from __future__ import annotations

__all__ = [
    "CollectionManifest",
    "build_manifest",
    "collect_events",
    "events_output_dir",
    "write_events_parquet",
    "write_manifest",
    "write_schema_doc",
]

import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import click
import httpx
import polars as pl

from ml.data_collection.http_client import GammaHttpClient

_DEFAULT_START_DATE_MIN = "2020-01-01T00:00:00Z"
_DEFAULT_PAGE_LIMIT = 100

# Column order and dtypes for the ``events/`` (Tier 1) dataset. Kept as a
# module constant so an empty collection still returns a correctly-typed
# (zero-row) DataFrame instead of an untyped one.
_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "venue": pl.Utf8,
    "series_id": pl.Utf8,
    "window_slug": pl.Utf8,
    "market_name": pl.Utf8,
    "window_name": pl.Utf8,
    "condition_id": pl.Utf8,
    "token_id_up": pl.Utf8,
    "token_id_down": pl.Utf8,
    "start_ts": pl.Datetime(time_unit="us", time_zone="UTC"),
    "end_ts": pl.Datetime(time_unit="us", time_zone="UTC"),
    # Money/price fields are stored as Decimal-compatible strings (never
    # float), per .claude/rules/00-stack.md rule 6.
    "price_to_beat": pl.Utf8,
    "final_price": pl.Utf8,
    "outcome": pl.Utf8,
    "lifetime_volume": pl.Utf8,
}

_SCHEMA_MD_PATH = Path(__file__).with_name("events_schema.md")


@dataclass(frozen=True)
class CollectionManifest:
    """Reproducible record of a single Tier 1 collection run.

    ``params_sha256`` is computed over collection parameters plus resulting
    row count and min/max ``start_ts`` — **not** over ``collected_at`` — so
    re-running an identical collection against unchanged upstream data
    produces a byte-identical hash.
    """

    venue: str
    series_id: str
    closed: bool
    start_date_min: str
    row_count: int
    min_start_ts: str | None
    max_start_ts: str | None
    collected_at: str
    params_sha256: str


def _decimal_str(value: Any) -> str | None:
    """Best-effort conversion of a raw JSON numeric/string field to a
    Decimal-compatible string, tolerating missing or malformed input.

    Args:
        value: Raw value from the Gamma JSON payload (``None``, ``str``,
            ``int``, or ``float``).

    Returns:
        A string that round-trips through ``decimal.Decimal``, or ``None``
        if ``value`` is missing or not numeric.
    """
    if value is None:
        return None
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _json_loads_or_none(value: Any) -> list[str] | None:
    """Parse a Gamma JSON-string-encoded array field, tolerating bad input."""
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None


def _parse_iso(value: Any) -> datetime | None:
    """Parse a Gamma ISO-8601 timestamp string, tolerating missing input."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _match_up_down_tokens(
    outcomes: list[str] | None, clob_token_ids: list[str] | None
) -> tuple[str | None, str | None]:
    """Match outcome names to CLOB token ids for an up/down-style market."""
    if not outcomes or not clob_token_ids or len(outcomes) != len(clob_token_ids):
        return None, None
    token_id_up: str | None = None
    token_id_down: str | None = None
    for name, token_id in zip(outcomes, clob_token_ids, strict=True):
        normalized = name.strip().lower()
        if normalized == "up":
            token_id_up = token_id
        elif normalized == "down":
            token_id_down = token_id
    return token_id_up, token_id_down


def _resolve_outcome(outcomes: list[str] | None, outcome_prices: list[str] | None) -> str | None:
    """Determine which outcome won from settlement `outcomePrices` (1.0 payout)."""
    if not outcomes or not outcome_prices or len(outcomes) != len(outcome_prices):
        return None
    for name, price in zip(outcomes, outcome_prices, strict=True):
        try:
            if Decimal(price) == 1:
                return name.strip().upper()
        except (InvalidOperation, TypeError, ValueError):
            continue
    return None


def _parse_event_row(event: dict[str, Any], *, venue: str, series_id: str) -> dict[str, Any] | None:
    """Convert one raw Gamma event dict into one ``events/`` row.

    Returns:
        A row dict matching ``_SCHEMA``'s keys, or ``None`` if the event has
        no embedded market (defensive — should not happen for this series,
        but a malformed event must never crash the whole collection).
    """
    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    series_list = event.get("series") or []
    market_name = series_list[0].get("title") if series_list else None

    outcomes = _json_loads_or_none(market.get("outcomes"))
    clob_token_ids = _json_loads_or_none(market.get("clobTokenIds"))
    outcome_prices = _json_loads_or_none(market.get("outcomePrices"))
    token_id_up, token_id_down = _match_up_down_tokens(outcomes, clob_token_ids)

    event_metadata = event.get("eventMetadata") or {}

    return {
        "venue": venue,
        "series_id": str(series_id),
        "window_slug": event.get("slug"),
        "market_name": market_name,
        "window_name": event.get("title"),
        "condition_id": market.get("conditionId"),
        "token_id_up": token_id_up,
        "token_id_down": token_id_down,
        "start_ts": _parse_iso(event.get("startTime")),
        "end_ts": _parse_iso(event.get("endDate")),
        "price_to_beat": _decimal_str(event_metadata.get("priceToBeat")),
        "final_price": _decimal_str(event_metadata.get("finalPrice")),
        "outcome": _resolve_outcome(outcomes, outcome_prices),
        "lifetime_volume": _decimal_str(market.get("volume")),
    }


def _iter_event_rows(
    client: GammaHttpClient,
    *,
    series_id: str,
    venue: str,
    closed: bool,
    start_date_min: str,
    page_limit: int,
) -> Iterator[dict[str, Any]]:
    """Yield parsed event rows via date-fenced offset pagination. See module docstring."""
    fence = start_date_min
    seen_slugs: set[str] = set()

    while True:
        offset = 0
        fence_max_start_date: str | None = None
        advanced_in_fence = False

        while True:
            params: dict[str, Any] = {
                "series_id": series_id,
                "closed": str(closed).lower(),
                "limit": page_limit,
                "offset": offset,
                "order": "startDate",
                "ascending": "true",
                "start_date_min": fence,
            }
            try:
                page = client.get("/events", params=params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 422:
                    break  # Offset ceiling hit for this fence — advance it.
                raise

            if not page:
                return  # Fully exhausted: no more windows at all.

            for raw_event in page:
                raw_start_date = raw_event.get("startDate")
                if isinstance(raw_start_date, str) and (
                    fence_max_start_date is None or raw_start_date > fence_max_start_date
                ):
                    fence_max_start_date = raw_start_date

                slug = raw_event.get("slug")
                if slug is None or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                advanced_in_fence = True

                row = _parse_event_row(raw_event, venue=venue, series_id=series_id)
                if row is not None:
                    yield row

            if len(page) < page_limit:
                return  # Last page of the entire result set.
            offset += page_limit

        if fence_max_start_date is None or not advanced_in_fence:
            return  # Nothing new in this fence — avoid an infinite loop.
        fence = fence_max_start_date


def collect_events(
    series_id: str,
    venue: str = "polymarket",
    closed: bool = True,
    *,
    start_date_min: str = _DEFAULT_START_DATE_MIN,
    page_limit: int = _DEFAULT_PAGE_LIMIT,
    client: GammaHttpClient | None = None,
) -> pl.DataFrame:
    """Collect every closed event/window for a Gamma recurring series.

    Args:
        series_id: The venue's series id (e.g. ``"10684"`` for BTC Up or
            Down 5m). Never hardcoded — always an explicit argument.
        venue: Venue discriminator, ``"polymarket"`` today. Forward-looking:
            other venues may be added later.
        closed: If ``True`` (default), collect only settled/closed windows.
        start_date_min: ISO-8601 lower bound on Gamma's internal ``startDate``
            ordering field, used to resume/limit a collection.
        page_limit: Page size for each underlying HTTP request.
        client: Optional pre-constructed :class:`GammaHttpClient` (e.g. for
            dependency injection in tests). A new one is created and closed
            internally if not supplied.

    Returns:
        A Polars DataFrame with one row per window, columns per ``_SCHEMA``,
        deduplicated by ``window_slug`` and sorted by ``start_ts``.
    """
    owns_client = client is None
    active_client = client or GammaHttpClient()
    try:
        rows = list(
            _iter_event_rows(
                active_client,
                series_id=series_id,
                venue=venue,
                closed=closed,
                start_date_min=start_date_min,
                page_limit=page_limit,
            )
        )
    finally:
        if owns_client:
            active_client.close()

    if not rows:
        return pl.DataFrame(schema=_SCHEMA)

    df = pl.DataFrame(rows, schema=_SCHEMA)
    return df.unique(subset=["window_slug"], keep="first").sort("start_ts")


def events_output_dir(root: Path, venue: str, series_slug: str) -> Path:
    """Return the partitioned ``events/`` directory for a ``(venue, series_slug)``."""
    return root / venue / series_slug / "events"


def write_schema_doc(root: Path, venue: str, series_slug: str) -> Path:
    """Write (or overwrite) the ``SCHEMA.md`` companion doc for ``events/``."""
    out_dir = events_output_dir(root, venue, series_slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    schema_path = out_dir / "SCHEMA.md"
    schema_path.write_text(_SCHEMA_MD_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return schema_path


def write_events_parquet(
    df: pl.DataFrame,
    *,
    root: Path,
    venue: str,
    series_slug: str,
    partition_date: date | None = None,
) -> Path:
    """Write ``df`` to the partitioned Parquet layout for one collection run.

    Layout: ``<root>/<venue>/<series_slug>/events/yyyy=/mm=/dd=/*.parquet``.

    Args:
        df: DataFrame produced by :func:`collect_events`.
        root: Dataset root (e.g. ``ml/data/raw``).
        venue: Venue discriminator (partition key).
        series_slug: Human-readable series slug (partition key).
        partition_date: Date this collection run happened on (UTC). Defaults
            to today.

    Returns:
        Path to the written Parquet file.
    """
    run_date = partition_date or datetime.now(UTC).date()
    out_dir = (
        events_output_dir(root, venue, series_slug)
        / f"yyyy={run_date:%Y}"
        / f"mm={run_date:%m}"
        / f"dd={run_date:%d}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    out_path = out_dir / f"events-{run_stamp}.parquet"
    df.write_parquet(out_path)
    return out_path


def build_manifest(
    df: pl.DataFrame,
    *,
    venue: str,
    series_id: str,
    closed: bool,
    start_date_min: str,
) -> CollectionManifest:
    """Build a reproducible manifest describing one collection run."""
    row_count = df.height
    min_start_ts = df["start_ts"].min() if row_count else None
    max_start_ts = df["start_ts"].max() if row_count else None
    min_start_ts_str = str(min_start_ts) if min_start_ts is not None else None
    max_start_ts_str = str(max_start_ts) if max_start_ts is not None else None

    hashed_payload = {
        "venue": venue,
        "series_id": series_id,
        "closed": closed,
        "start_date_min": start_date_min,
        "row_count": row_count,
        "min_start_ts": min_start_ts_str,
        "max_start_ts": max_start_ts_str,
    }
    canonical = json.dumps(hashed_payload, sort_keys=True, separators=(",", ":"))
    params_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return CollectionManifest(
        venue=venue,
        series_id=series_id,
        closed=closed,
        start_date_min=start_date_min,
        row_count=row_count,
        min_start_ts=min_start_ts_str,
        max_start_ts=max_start_ts_str,
        collected_at=datetime.now(UTC).isoformat(),
        params_sha256=params_sha256,
    )


def write_manifest(
    manifest: CollectionManifest, *, root: Path, venue: str, series_slug: str
) -> Path:
    """Write a :class:`CollectionManifest` as JSON alongside the Parquet output."""
    out_dir = events_output_dir(root, venue, series_slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(asdict(manifest), indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest_path


@click.command()
@click.option(
    "--series-id",
    default="10684",
    show_default=True,
    help="Gamma series id to collect. Default is BTC Up or Down 5m for convenience.",
)
@click.option(
    "--series-slug",
    default="btc-up-or-down-5m",
    show_default=True,
    help="Series slug used for the output path partition.",
)
@click.option("--venue", default="polymarket", show_default=True)
@click.option("--closed/--open", "closed", default=True, help="Collect closed (settled) windows.")
@click.option(
    "--start-date-min",
    default=_DEFAULT_START_DATE_MIN,
    show_default=True,
    help="ISO-8601 lower bound on Gamma's internal startDate ordering field.",
)
@click.option(
    "--output-root",
    type=click.Path(path_type=Path),
    default=Path("ml/data/raw"),
    show_default=True,
)
def main(
    series_id: str,
    series_slug: str,
    venue: str,
    closed: bool,
    start_date_min: str,
    output_root: Path,
) -> None:
    """Collect Tier 1 (events) data for a Gamma recurring-market series."""
    df = collect_events(series_id, venue=venue, closed=closed, start_date_min=start_date_min)
    write_schema_doc(output_root, venue, series_slug)
    out_path = write_events_parquet(df, root=output_root, venue=venue, series_slug=series_slug)
    manifest = build_manifest(
        df, venue=venue, series_id=series_id, closed=closed, start_date_min=start_date_min
    )
    manifest_path = write_manifest(manifest, root=output_root, venue=venue, series_slug=series_slug)
    click.echo(f"Wrote {df.height} rows to {out_path}")
    click.echo(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
