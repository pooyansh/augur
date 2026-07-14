"""Tier 2 collector — per-second price/volume/trade-count from raw trades.

Reconstructs a genuine per-second price series for each outcome token of a
settlement window from Polymarket's raw trade log
(``data-api.polymarket.com/trades?market=<condition_id>``), since neither
Gamma nor CLOB's ``/prices-history`` emits a true fixed-grid per-second feed
(``/prices-history`` only emits a point when price actually moves — verified
empirically, see the plan doc). This is expensive relative to Tier 1 (one
window can produce thousands of trades, paginated 500 rows/page), so this
module is built to be interrupted and resumed via a checkpoint log rather
than assumed to run start-to-finish in one sitting.

Generality note (same constraint as ``events.py``): every public function
takes ``condition_id`` / token ids / window timing as explicit parameters.
Nothing about the BTC Up or Down 5m series is hardcoded here; that series is
only the first dataset this tooling has been exercised against.

Bucketing algorithm
--------------------
1. Fully paginate ``/trades?market=<condition_id>&limit=500&offset=N`` until
   a page comes back shorter than ``limit`` (including empty) — the trade log
   for one 5-minute-window market is small enough that the whole thing is
   held in memory for the duration of one window's collection.
2. Filter trades to ``[start_ts, start_ts + window_seconds)`` — defensive
   only; in practice a window's market only ever trades during its own
   window, but we don't assume that guarantee holds for every venue/series
   this module might be pointed at later.
3. Group by ``(token_id, second_offset)`` where
   ``second_offset = trade.timestamp - start_ts_epoch``. Per bucket:
   ``price`` = the last trade's price, ``volume`` = sum of trade sizes,
   ``trade_count`` = number of trades.
4. Emit one row per ``second_offset`` in ``range(window_seconds)`` per token
   — not just seconds with trades. A second with no trade forward-fills
   ``price`` from the most recent prior second that had one (same token);
   ``volume`` is ``0`` and ``trade_count`` is ``0`` (never null — "no trade"
   is a real, countable observation). Leading seconds before the very first
   trade for a token are left with ``price = None`` — deliberately not
   fabricated. Seeding those leading seconds from a pre-window price anchor
   is Tier 3 (``price_anchor/``), explicitly out of scope here.

Trade timestamps from this API have one-second resolution only (no
sub-second field observed), so within a single second the "last" trade is
whatever order the API returned it in — see ``seconds_schema.md`` for the
same caveat documented alongside the dataset.
"""

from __future__ import annotations

__all__ = [
    "CollectionSummary",
    "TradesManifest",
    "build_manifest",
    "collect_window_trades",
    "collect_windows",
    "load_checkpoint",
    "seconds_output_dir",
    "write_manifest",
    "write_schema_doc",
    "write_window_trades_parquet",
]

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import click
import httpx
import polars as pl

from ml.data_collection.http_client import TradesApiHttpClient

_DEFAULT_PAGE_LIMIT = 500
_DEFAULT_WINDOW_SECONDS = 300

# Column order and dtypes for the ``seconds/`` (Tier 2) dataset. A module
# constant so an empty/degenerate window still returns a correctly-typed
# DataFrame rather than an untyped one.
_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "window_slug": pl.Utf8,
    "market_name": pl.Utf8,
    "window_name": pl.Utf8,
    "token_id": pl.Utf8,
    "outcome": pl.Utf8,
    "second_offset": pl.Int64,
    "ts": pl.Datetime(time_unit="us", time_zone="UTC"),
    # Money/size fields are stored as Decimal-compatible strings (never
    # float), per .claude/rules/00-stack.md rule 6.
    "price": pl.Utf8,
    "volume": pl.Utf8,
    "trade_count": pl.Int64,
}

_SCHEMA_MD_PATH = Path(__file__).with_name("seconds_schema.md")


@dataclass(frozen=True)
class ParsedTrade:
    """One raw trade, defensively parsed to typed fields."""

    asset: str
    price: Decimal
    size: Decimal
    timestamp: int


@dataclass(frozen=True)
class WindowCheckpointRecord:
    """One completed-window entry in the resumable checkpoint log."""

    window_slug: str
    row_count: int
    start_ts: str
    completed_at: str
    # True when the Data API's undocumented offset ceiling (see
    # _fetch_all_trades) cut off pagination before genuinely exhausting this
    # market's trade history — this window's earliest trades may be missing.
    truncated: bool = False


@dataclass(frozen=True)
class CollectionSummary:
    """Outcome of one :func:`collect_windows` batch run."""

    windows_total: int
    windows_processed_this_run: int
    windows_skipped_already_done: int
    row_count_this_run: int
    windows_truncated_this_run: int = 0


@dataclass(frozen=True)
class TradesManifest:
    """Reproducible record of the cumulative Tier 2 collection state.

    Built from the checkpoint log (not from re-reading every Parquet file),
    so building it stays cheap even once thousands of windows are done.
    ``params_sha256`` excludes ``collected_at`` so re-running against
    unchanged checkpoint state produces a byte-identical hash.
    """

    venue: str
    series_slug: str
    window_count_total_this_run: int
    window_count_completed: int
    row_count_completed: int
    min_start_ts: str | None
    max_start_ts: str | None
    collected_at: str
    params_sha256: str


def _decimal_str(value: Any) -> str | None:
    """Best-effort conversion of a raw JSON numeric field to a Decimal string."""
    if value is None:
        return None
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_trade(raw: dict[str, Any]) -> ParsedTrade | None:
    """Parse one raw trade dict, tolerating malformed input (returns ``None``)."""
    asset = raw.get("asset")
    if not isinstance(asset, str):
        return None
    price_str = _decimal_str(raw.get("price"))
    size_str = _decimal_str(raw.get("size"))
    timestamp = raw.get("timestamp")
    if price_str is None or size_str is None or not isinstance(timestamp, int):
        return None
    return ParsedTrade(
        asset=asset, price=Decimal(price_str), size=Decimal(size_str), timestamp=timestamp
    )


_MAX_OFFSET_ERROR_SUBSTRING = "max historical activity offset"


def _fetch_all_trades(
    client: TradesApiHttpClient, *, condition_id: str, page_limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """Fully paginate ``/trades`` for one market.

    Stops on the first page shorter than ``page_limit`` (including empty) —
    the documented signal that the result set is exhausted.

    **Discovered API ceiling (undocumented, verified empirically 2026-07-12):**
    the Data API refuses any ``offset > 3000`` with HTTP 400
    ``{"error": "max historical activity offset of 3000 exceeded"}``, capping
    the maximum retrievable trades per market at ``3000 + page_limit`` (e.g.
    3500 at the default 500-row page). Trades are returned newest-first, so
    a truncated market's *earliest* trades in the window are the ones lost —
    this can push the leading-null stretch in ``seconds/`` further into the
    window than the true first trade, for windows active enough to exceed
    the ceiling. No working fencing parameter (``before``/``after`` are
    silently ignored — verified empirically) was found to work around this,
    unlike Gamma's offset ceiling in ``events.py``. Flagged, not silently
    swallowed: the second return value is ``True`` when truncation occurred,
    so callers can record it (see ``WindowCheckpointRecord.truncated``).

    Returns:
        ``(trades, truncated)`` — all raw trade dicts retrievable for this
        market, and whether pagination was cut short by the offset ceiling
        rather than reaching a genuine end of results.
    """
    trades: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {"market": condition_id, "limit": page_limit, "offset": offset}
        try:
            page = client.get("/trades", params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 and _MAX_OFFSET_ERROR_SUBSTRING in exc.response.text:
                return trades, True
            raise
        if not page:
            return trades, False
        trades.extend(page)
        if len(page) < page_limit:
            return trades, False
        offset += page_limit


def _bucket_trades(
    trades: list[ParsedTrade], *, token_id: str, start_epoch: int, window_seconds: int
) -> dict[int, tuple[Decimal, Decimal, int]]:
    """Group one token's trades by second-offset within the window.

    Returns:
        Mapping of ``second_offset -> (last_price, summed_volume, trade_count)``
        for seconds that actually had at least one trade. Seconds outside
        ``[0, window_seconds)`` (defensive only — see module docstring) are
        dropped.
    """
    grouped: dict[int, list[ParsedTrade]] = {}
    for trade in trades:
        if trade.asset != token_id:
            continue
        offset = trade.timestamp - start_epoch
        if 0 <= offset < window_seconds:
            grouped.setdefault(offset, []).append(trade)

    buckets: dict[int, tuple[Decimal, Decimal, int]] = {}
    for offset, group in grouped.items():
        volume = sum((t.size for t in group), start=Decimal("0"))
        buckets[offset] = (group[-1].price, volume, len(group))
    return buckets


def _build_second_rows(
    buckets: dict[int, tuple[Decimal, Decimal, int]],
    *,
    token_id: str,
    outcome: str | None,
    window_slug: str,
    market_name: str | None,
    window_name: str | None,
    start_ts: datetime,
    window_seconds: int,
) -> list[dict[str, Any]]:
    """Expand one token's trade buckets into a full 0..window_seconds-1 row range."""
    rows: list[dict[str, Any]] = []
    last_price: Decimal | None = None
    for offset in range(window_seconds):
        bucketed = buckets.get(offset)
        price: Decimal | None
        if bucketed is not None:
            price, volume, trade_count = bucketed
            last_price = price
        else:
            # No trade this second: forward-fill price from the most recent
            # prior second that had one (or leave null if none has occurred
            # yet — see module docstring; not fabricated).
            price, volume, trade_count = last_price, Decimal("0"), 0
        rows.append(
            {
                "window_slug": window_slug,
                "market_name": market_name,
                "window_name": window_name,
                "token_id": token_id,
                "outcome": outcome,
                "second_offset": offset,
                "ts": start_ts + timedelta(seconds=offset),
                "price": str(price) if price is not None else None,
                "volume": str(volume),
                "trade_count": trade_count,
            }
        )
    return rows


def collect_window_trades(
    condition_id: str,
    token_id_up: str,
    token_id_down: str,
    window_slug: str,
    start_ts: datetime,
    *,
    market_name: str | None = None,
    window_name: str | None = None,
    outcome: str | None = None,
    window_seconds: int = _DEFAULT_WINDOW_SECONDS,
    page_limit: int = _DEFAULT_PAGE_LIMIT,
    client: TradesApiHttpClient | None = None,
) -> pl.DataFrame:
    """Reconstruct the per-second price/volume/trade-count series for one window.

    Args:
        condition_id: The venue's market id for this window (Tier 1's
            ``condition_id``). Never hardcoded — always an explicit argument.
        token_id_up: Outcome token id for the "up" side.
        token_id_down: Outcome token id for the "down" side.
        window_slug: Unique id for the window (joins back to ``events/``).
        start_ts: Window open time (UTC, tz-aware).
        market_name: Denormalized series name, e.g. ``"BTC Up or Down 5m"``.
        window_name: Denormalized per-window human-readable title.
        outcome: Which side won the window (denormalized onto every row).
        window_seconds: Window duration in seconds (``300`` for a 5-minute
            window). Never assumed — always an explicit argument so this
            generalizes to other cadences.
        page_limit: Page size for each underlying HTTP request.
        client: Optional pre-constructed :class:`TradesApiHttpClient` (e.g.
            for dependency injection / connection reuse across many windows
            in :func:`collect_windows`). A new one is created and closed
            internally if not supplied.

    Returns:
        A Polars DataFrame with ``2 * window_seconds`` rows (one per token
        per second), columns per ``_SCHEMA``. See :func:`_collect_window_trades_impl`
        if the offset-ceiling truncation flag is also needed (used internally
        by :func:`collect_windows` to record it in the checkpoint).
    """
    df, _truncated = _collect_window_trades_impl(
        condition_id,
        token_id_up,
        token_id_down,
        window_slug,
        start_ts,
        market_name=market_name,
        window_name=window_name,
        outcome=outcome,
        window_seconds=window_seconds,
        page_limit=page_limit,
        client=client,
    )
    return df


def _collect_window_trades_impl(
    condition_id: str,
    token_id_up: str,
    token_id_down: str,
    window_slug: str,
    start_ts: datetime,
    *,
    market_name: str | None = None,
    window_name: str | None = None,
    outcome: str | None = None,
    window_seconds: int = _DEFAULT_WINDOW_SECONDS,
    page_limit: int = _DEFAULT_PAGE_LIMIT,
    client: TradesApiHttpClient | None = None,
) -> tuple[pl.DataFrame, bool]:
    """Implementation behind :func:`collect_window_trades`; also surfaces truncation."""
    owns_client = client is None
    active_client = client or TradesApiHttpClient()
    try:
        raw_trades, truncated = _fetch_all_trades(
            active_client, condition_id=condition_id, page_limit=page_limit
        )
    finally:
        if owns_client:
            active_client.close()

    parsed_trades = [t for t in (_parse_trade(raw) for raw in raw_trades) if t is not None]
    start_epoch = int(start_ts.timestamp())

    rows: list[dict[str, Any]] = []
    for token_id in (token_id_up, token_id_down):
        buckets = _bucket_trades(
            parsed_trades, token_id=token_id, start_epoch=start_epoch, window_seconds=window_seconds
        )
        rows.extend(
            _build_second_rows(
                buckets,
                token_id=token_id,
                outcome=outcome,
                window_slug=window_slug,
                market_name=market_name,
                window_name=window_name,
                start_ts=start_ts,
                window_seconds=window_seconds,
            )
        )
    return pl.DataFrame(rows, schema=_SCHEMA), truncated


def seconds_output_dir(root: Path, venue: str, series_slug: str) -> Path:
    """Return the partitioned ``seconds/`` directory for a ``(venue, series_slug)``."""
    return root / venue / series_slug / "seconds"


def write_schema_doc(root: Path, venue: str, series_slug: str) -> Path:
    """Write (or overwrite) the ``SCHEMA.md`` companion doc for ``seconds/``."""
    out_dir = seconds_output_dir(root, venue, series_slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    schema_path = out_dir / "SCHEMA.md"
    schema_path.write_text(_SCHEMA_MD_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return schema_path


def write_window_trades_parquet(
    df: pl.DataFrame,
    *,
    root: Path,
    venue: str,
    series_slug: str,
    window_slug: str,
    window_date: date,
) -> Path:
    """Write one window's per-second DataFrame to the partitioned Parquet layout.

    Layout: ``<root>/<venue>/<series_slug>/seconds/yyyy=/mm=/dd=/*.parquet``,
    partitioned by the *window's own* start date (unlike Tier 1's ``events/``,
    which partitions by collection run date) — one window's file always lands
    in the same partition regardless of when it was collected. The filename
    is deterministic on ``window_slug`` (no run timestamp), so re-collecting
    the same window overwrites rather than accumulates duplicates.

    Args:
        df: DataFrame produced by :func:`collect_window_trades`.
        root: Dataset root (e.g. ``ml/data/raw``).
        venue: Venue discriminator (partition key).
        series_slug: Human-readable series slug (partition key).
        window_slug: Unique window id, used as the filename stem.
        window_date: The window's own UTC start date (partition key).

    Returns:
        Path to the written Parquet file.
    """
    out_dir = (
        seconds_output_dir(root, venue, series_slug)
        / f"yyyy={window_date:%Y}"
        / f"mm={window_date:%m}"
        / f"dd={window_date:%d}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"seconds-{window_slug}.parquet"
    df.write_parquet(out_path)
    return out_path


def load_checkpoint(checkpoint_path: Path) -> set[str]:
    """Return the set of ``window_slug``s already fully collected and written."""
    if not checkpoint_path.exists():
        return set()
    done: set[str] = set()
    for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        done.add(record["window_slug"])
    return done


def _read_checkpoint_records(checkpoint_path: Path) -> list[dict[str, Any]]:
    if not checkpoint_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _append_checkpoint(checkpoint_path: Path, record: WindowCheckpointRecord) -> None:
    """Append one completed-window record to the checkpoint log (JSON Lines)."""
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with checkpoint_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record), sort_keys=True) + "\n")


def collect_windows(
    events_df: pl.DataFrame,
    *,
    output_root: Path,
    checkpoint_path: Path,
    venue: str = "polymarket",
    series_slug: str = "btc-up-or-down-5m",
    window_seconds: int = _DEFAULT_WINDOW_SECONDS,
    page_limit: int = _DEFAULT_PAGE_LIMIT,
    client: TradesApiHttpClient | None = None,
) -> CollectionSummary:
    """Batch-drive Tier 2 collection over every window in a Tier 1 DataFrame.

    Resumable: any ``window_slug`` already present in ``checkpoint_path`` is
    skipped without an HTTP call. Each window is written to its own Parquet
    file and checkpointed immediately after a successful write, so an
    interrupted run loses at most the one window in flight — memory stays
    bounded regardless of how many windows are processed in total (nothing
    is accumulated in-process across windows).

    Args:
        events_df: A Tier-1-shaped DataFrame (see ``events.py``), already
            filtered by the caller to whatever window range should be
            collected (e.g. a 2-week slice for the pilot).
        output_root: Dataset root (e.g. ``ml/data/raw``).
        checkpoint_path: Path to the JSON-Lines checkpoint log.
        venue: Venue discriminator (output partition key).
        series_slug: Series slug (output partition key).
        window_seconds: Window duration in seconds.
        page_limit: Page size for each underlying HTTP request.
        client: Optional pre-constructed :class:`TradesApiHttpClient`, reused
            across all windows in this run (a fresh one is created and
            closed internally if not supplied).

    Returns:
        A :class:`CollectionSummary` describing this run.
    """
    owns_client = client is None
    active_client = client or TradesApiHttpClient()
    already_done = load_checkpoint(checkpoint_path)
    windows_total = events_df.height
    processed = 0
    skipped = 0
    total_rows = 0
    truncated_count = 0
    try:
        for row in events_df.sort("start_ts").iter_rows(named=True):
            window_slug = row["window_slug"]
            if window_slug in already_done:
                skipped += 1
                continue

            condition_id = row["condition_id"]
            token_id_up = row["token_id_up"]
            token_id_down = row["token_id_down"]
            start_ts = row["start_ts"]
            if not (condition_id and token_id_up and token_id_down and start_ts is not None):
                # Malformed Tier 1 row (missing ids) — cannot collect trades
                # for it. Record as done with zero rows so a resumed run
                # doesn't retry it forever; it will never become collectible.
                _append_checkpoint(
                    checkpoint_path,
                    WindowCheckpointRecord(
                        window_slug=window_slug,
                        row_count=0,
                        start_ts=str(start_ts) if start_ts is not None else "",
                        completed_at=datetime.now(UTC).isoformat(),
                    ),
                )
                skipped += 1
                continue

            df, truncated = _collect_window_trades_impl(
                condition_id,
                token_id_up,
                token_id_down,
                window_slug,
                start_ts,
                market_name=row.get("market_name"),
                window_name=row.get("window_name"),
                outcome=row.get("outcome"),
                window_seconds=window_seconds,
                page_limit=page_limit,
                client=active_client,
            )
            write_window_trades_parquet(
                df,
                root=output_root,
                venue=venue,
                series_slug=series_slug,
                window_slug=window_slug,
                window_date=start_ts.date(),
            )
            _append_checkpoint(
                checkpoint_path,
                WindowCheckpointRecord(
                    window_slug=window_slug,
                    row_count=df.height,
                    start_ts=start_ts.isoformat(),
                    completed_at=datetime.now(UTC).isoformat(),
                    truncated=truncated,
                ),
            )
            processed += 1
            total_rows += df.height
            if truncated:
                truncated_count += 1
    finally:
        if owns_client:
            active_client.close()

    return CollectionSummary(
        windows_total=windows_total,
        windows_processed_this_run=processed,
        windows_skipped_already_done=skipped,
        row_count_this_run=total_rows,
        windows_truncated_this_run=truncated_count,
    )


def build_manifest(
    checkpoint_path: Path, *, venue: str, series_slug: str, window_count_total_this_run: int
) -> TradesManifest:
    """Build a reproducible manifest from the cumulative checkpoint state."""
    records = _read_checkpoint_records(checkpoint_path)
    row_count = sum(r["row_count"] for r in records)
    start_ts_values = sorted(r["start_ts"] for r in records if r["start_ts"])
    min_start_ts = start_ts_values[0] if start_ts_values else None
    max_start_ts = start_ts_values[-1] if start_ts_values else None
    window_count_completed = len(records)

    hashed_payload = {
        "venue": venue,
        "series_slug": series_slug,
        "window_count_completed": window_count_completed,
        "row_count_completed": row_count,
        "min_start_ts": min_start_ts,
        "max_start_ts": max_start_ts,
    }
    canonical = json.dumps(hashed_payload, sort_keys=True, separators=(",", ":"))
    params_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return TradesManifest(
        venue=venue,
        series_slug=series_slug,
        window_count_total_this_run=window_count_total_this_run,
        window_count_completed=window_count_completed,
        row_count_completed=row_count,
        min_start_ts=min_start_ts,
        max_start_ts=max_start_ts,
        collected_at=datetime.now(UTC).isoformat(),
        params_sha256=params_sha256,
    )


def write_manifest(manifest: TradesManifest, *, root: Path, venue: str, series_slug: str) -> Path:
    """Write a :class:`TradesManifest` as JSON alongside the Parquet output."""
    out_dir = seconds_output_dir(root, venue, series_slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(asdict(manifest), indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest_path


@click.command()
@click.option(
    "--events-parquet",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help="Path to a Tier 1 events Parquet file (from events.py).",
)
@click.option("--start-date-min", default=None, help="ISO-8601 inclusive lower bound on start_ts.")
@click.option("--start-date-max", default=None, help="ISO-8601 exclusive upper bound on start_ts.")
@click.option("--venue", default="polymarket", show_default=True)
@click.option("--series-slug", default="btc-up-or-down-5m", show_default=True)
@click.option("--window-seconds", default=_DEFAULT_WINDOW_SECONDS, show_default=True)
@click.option("--rate-per-sec", default=5.0, show_default=True, help="Data API request rate cap.")
@click.option(
    "--output-root", type=click.Path(path_type=Path), default=Path("ml/data/raw"), show_default=True
)
@click.option(
    "--checkpoint-path",
    type=click.Path(path_type=Path),
    default=None,
    help="Defaults to <output-root>/<venue>/<series-slug>/seconds/checkpoint.jsonl",
)
@click.option(
    "--max-windows",
    type=int,
    default=None,
    help="Optional cap on windows processed this run (for pilot/smoke runs).",
)
def main(
    events_parquet: Path,
    start_date_min: str | None,
    start_date_max: str | None,
    venue: str,
    series_slug: str,
    window_seconds: int,
    rate_per_sec: float,
    output_root: Path,
    checkpoint_path: Path | None,
    max_windows: int | None,
) -> None:
    """Collect Tier 2 (per-second) data for a range of windows from a Tier 1 file."""
    events_df = pl.read_parquet(events_parquet)
    if start_date_min is not None:
        events_df = events_df.filter(pl.col("start_ts") >= datetime.fromisoformat(start_date_min))
    if start_date_max is not None:
        events_df = events_df.filter(pl.col("start_ts") < datetime.fromisoformat(start_date_max))
    events_df = events_df.sort("start_ts")
    if max_windows is not None:
        events_df = events_df.head(max_windows)

    resolved_checkpoint = checkpoint_path or (
        seconds_output_dir(output_root, venue, series_slug) / "checkpoint.jsonl"
    )
    write_schema_doc(output_root, venue, series_slug)

    with TradesApiHttpClient(rate_per_sec=rate_per_sec) as client:
        summary = collect_windows(
            events_df,
            output_root=output_root,
            checkpoint_path=resolved_checkpoint,
            venue=venue,
            series_slug=series_slug,
            window_seconds=window_seconds,
            client=client,
        )

    manifest = build_manifest(
        resolved_checkpoint,
        venue=venue,
        series_slug=series_slug,
        window_count_total_this_run=summary.windows_total,
    )
    manifest_path = write_manifest(manifest, root=output_root, venue=venue, series_slug=series_slug)

    click.echo(
        f"Processed {summary.windows_processed_this_run} windows "
        f"({summary.windows_skipped_already_done} already done, "
        f"{summary.row_count_this_run} rows written this run, "
        f"{summary.windows_truncated_this_run} truncated by the Data API offset ceiling)."
    )
    click.echo(f"Checkpoint: {resolved_checkpoint}")
    click.echo(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
