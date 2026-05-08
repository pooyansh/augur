"""
Polymarket scout — Phase 1 throwaway CLI.

Probes the Polymarket public CLOB and Gamma APIs and captures raw findings to runs/.
No auth, no retries (except WS reconnect), no abstractions. The outputs are the point.

Usage: python polymarket_scout.py <subcommand> [args]
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import orjson
import websockets
from tenacity import retry, stop_after_attempt, wait_fixed

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

RUNS_DIR = Path(__file__).parent / "runs"

# ---------------------------------------------------------------------------
# Redaction filter — masks any value from env vars matching POLYMARKET_*_KEY
# or *_PRIVATE_*. In Phase 1 we have no keys, but the wiring is mandatory per
# the plan's "one piece of production hygiene we keep".
# ---------------------------------------------------------------------------

def _build_redaction_patterns() -> list[re.Pattern[str]]:
    patterns = []
    for key, val in os.environ.items():
        if re.search(r"POLYMARKET_.+_KEY|.+_PRIVATE_.+", key) and val:
            patterns.append(re.compile(re.escape(val)))
    return patterns


_REDACT_PATTERNS: list[re.Pattern[str]] = _build_redaction_patterns()


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat in _REDACT_PATTERNS:
            msg = pat.sub("***", msg)
        record.msg = msg
        record.args = ()
        return True


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("scout")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
    return logger


log = _setup_logging()

# ---------------------------------------------------------------------------
# Capture helpers
# ---------------------------------------------------------------------------

@dataclass
class CapturedRequest:
    url: str
    method: str
    request_body: Any
    response_status: int
    response_headers: dict[str, str]
    response_body: Any
    latency_ms: float
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _open_capture(subcmd: str) -> tuple[Path, Any]:
    RUNS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS_DIR / f"{ts}-{subcmd}.jsonl"
    fh = path.open("wb")
    return path, fh


def _write_capture(fh: Any, capture: CapturedRequest) -> None:
    line = orjson.dumps({
        "ts": capture.ts,
        "request": {"method": capture.method, "url": capture.url, "body": capture.request_body},
        "response_status": capture.response_status,
        "response_headers": capture.response_headers,
        "response_body": capture.response_body,
        "latency_ms": capture.latency_ms,
    }) + b"\n"
    fh.write(line)
    fh.flush()


async def _get(
    client: httpx.AsyncClient,
    url: str,
    fh: Any,
    *,
    params: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """GET url, capture to fh, return (status_code, parsed_body)."""
    t0 = time.perf_counter()
    try:
        resp = await client.get(url, params=params)
    except httpx.RequestError as exc:
        log.error("Request error: %s", exc)
        raise
    latency_ms = (time.perf_counter() - t0) * 1000

    try:
        body = orjson.loads(resp.content)
    except Exception:
        body = resp.text

    capture = CapturedRequest(
        url=str(resp.url),
        method="GET",
        request_body=None,
        response_status=resp.status_code,
        response_headers=dict(resp.headers),
        response_body=body,
        latency_ms=round(latency_ms, 2),
    )
    _write_capture(fh, capture)
    return resp.status_code, body


async def _post_raw(
    client: httpx.AsyncClient,
    url: str,
    fh: Any,
    *,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """POST url (possibly with no body), capture to fh."""
    t0 = time.perf_counter()
    try:
        resp = await client.post(url, json=json_body)
    except httpx.RequestError as exc:
        log.error("Request error: %s", exc)
        raise
    latency_ms = (time.perf_counter() - t0) * 1000

    try:
        body = orjson.loads(resp.content)
    except Exception:
        body = resp.text

    capture = CapturedRequest(
        url=str(resp.url),
        method="POST",
        request_body=json_body,
        response_status=resp.status_code,
        response_headers=dict(resp.headers),
        response_body=body,
        latency_ms=round(latency_ms, 2),
    )
    _write_capture(fh, capture)
    return resp.status_code, body


def _print(obj: Any) -> None:
    print(orjson.dumps(obj, option=orjson.OPT_INDENT_2).decode())


def _new_client() -> httpx.AsyncClient:
    # http2=True: Polymarket's CLOB CDN supports HTTP/2; reduces connection overhead
    # on repeated requests during bursts and streaming probes.
    return httpx.AsyncClient(http2=True, timeout=30.0)


# ---------------------------------------------------------------------------
# Subcommand: list-markets
# ---------------------------------------------------------------------------

async def cmd_list_markets(args: argparse.Namespace) -> None:
    cap_path, fh = _open_capture("list-markets")
    log.info("Capturing to %s", cap_path)

    params: dict[str, Any] = {"limit": args.limit, "offset": args.offset}
    if args.closed:
        params["closed"] = "true"
    else:
        params["active"] = "true"
        params["closed"] = "false"

    async with _new_client() as client:
        status, body = await _get(client, f"{GAMMA_BASE}/markets", fh, params=params)
    fh.close()

    log.info("Status %d, %d markets returned", status, len(body) if isinstance(body, list) else -1)

    if not isinstance(body, list):
        _print(body)
        sys.exit(1 if status >= 400 else 0)

    # Compact table
    rows = []
    for m in body:
        token_ids = m.get("clobTokenIds") or []
        rows.append({
            "condition_id": m.get("conditionId", ""),
            "slug": m.get("slug", ""),
            "end_date": m.get("endDate", ""),
            "volume": m.get("volume", ""),
            "token_id_0": token_ids[0] if len(token_ids) > 0 else "",
            "token_id_1": token_ids[1] if len(token_ids) > 1 else "",
        })

    # Print table header + rows
    cols = ["condition_id", "slug", "end_date", "volume", "token_id_0", "token_id_1"]
    widths = {c: max(len(c), max((len(str(r[c])) for r in rows), default=0)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))

    print(f"\n[{len(rows)} markets, captured to {cap_path}]")


# ---------------------------------------------------------------------------
# Subcommand: get-market
# ---------------------------------------------------------------------------

async def cmd_get_market(args: argparse.Namespace) -> None:
    cid = args.condition_id
    cap_path, fh = _open_capture("get-market")
    log.info("Fetching market %s from both APIs, capturing to %s", cid, cap_path)

    async with _new_client() as client:
        clob_status, clob_body = await _get(client, f"{CLOB_BASE}/markets/{cid}", fh)
        gamma_status, gamma_body = await _get(
            client, f"{GAMMA_BASE}/markets", fh, params={"condition_ids": cid}
        )
    fh.close()

    print("\n=== CLOB response (status %d) ===" % clob_status)
    _print(clob_body)

    print("\n=== Gamma response (status %d) ===" % gamma_status)
    _print(gamma_body)

    # Schema diff: keys present in one but not the other
    clob_keys: set[str] = set(clob_body.keys()) if isinstance(clob_body, dict) else set()
    gamma_record = gamma_body[0] if isinstance(gamma_body, list) and gamma_body else {}
    gamma_keys: set[str] = set(gamma_record.keys()) if isinstance(gamma_record, dict) else set()

    print("\n=== Schema diff ===")
    only_clob = sorted(clob_keys - gamma_keys)
    only_gamma = sorted(gamma_keys - clob_keys)
    if only_clob:
        print(f"Only in CLOB ({len(only_clob)}): {only_clob}")
    if only_gamma:
        print(f"Only in Gamma ({len(only_gamma)}): {only_gamma}")
    if not only_clob and not only_gamma:
        print("(key sets are identical)")

    print(f"\n[Captured to {cap_path}]")


# ---------------------------------------------------------------------------
# Subcommand: get-orderbook
# ---------------------------------------------------------------------------

async def cmd_get_orderbook(args: argparse.Namespace) -> None:
    token_id = args.token_id
    cap_path, fh = _open_capture("get-orderbook")
    log.info("Fetching orderbook for token_id=%s", token_id)

    async with _new_client() as client:
        t0 = time.perf_counter()
        status, body = await _get(
            client, f"{CLOB_BASE}/book", fh, params={"token_id": token_id}
        )
        latency_ms = (time.perf_counter() - t0) * 1000
    fh.close()

    log.info("Status %d, latency %.1f ms", status, latency_ms)

    if status >= 400 or not isinstance(body, dict):
        _print(body)
        sys.exit(1)

    bids = body.get("bids", [])
    asks = body.get("asks", [])

    # Bids sorted descending by price, asks ascending
    def to_float(x: Any) -> float:
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    bids_sorted = sorted(bids, key=lambda x: to_float(x.get("price", 0)), reverse=True)[:5]
    asks_sorted = sorted(asks, key=lambda x: to_float(x.get("price", 0)))[:5]

    best_bid = to_float(bids_sorted[0].get("price", 0)) if bids_sorted else None
    best_ask = to_float(asks_sorted[0].get("price", 0)) if asks_sorted else None

    print(f"\nOrderbook for token_id={token_id}")
    print(f"Round-trip latency: {latency_ms:.1f} ms")
    print(f"\nTop 5 ASKS (ascending):")
    for level in asks_sorted:
        print(f"  price={level.get('price')}  size={level.get('size')}")
    print(f"\nTop 5 BIDS (descending):")
    for level in bids_sorted:
        print(f"  price={level.get('price')}  size={level.get('size')}")

    if best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2
        print(f"\nBest bid: {best_bid}  Best ask: {best_ask}  Spread: {spread:.4f}  Mid: {mid:.4f}")
    else:
        print(f"\nbest_bid={best_bid}  best_ask={best_ask}  (one or both sides empty)")

    print(f"\n[Captured to {cap_path}]")


# ---------------------------------------------------------------------------
# Subcommand: get-trades
# ---------------------------------------------------------------------------

async def cmd_get_trades(args: argparse.Namespace) -> None:
    cid = args.condition_id
    cap_path, fh = _open_capture("get-trades")
    log.info("Fetching trades for market=%s limit=%d", cid, args.limit)

    async with _new_client() as client:
        status, body = await _get(
            client,
            f"{CLOB_BASE}/trades",
            fh,
            params={"market": cid, "limit": args.limit},
        )
    fh.close()

    log.info("Status %d", status)
    # Phase 1 intent: if endpoint requires auth we capture and print the actual error — don't swallow.
    _print(body)
    print(f"\n[Status {status}, captured to {cap_path}]")
    if status >= 400:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: watch-stream
# ---------------------------------------------------------------------------

async def _stream_inner(
    token_ids: list[str],
    duration: int,
    fh: Any,
) -> None:
    """Inner WS loop. Called by tenacity retry wrapper."""
    subscribe_msg = orjson.dumps({"type": "market", "assets_ids": token_ids}).decode()

    async with websockets.connect(WS_URL, ping_interval=20) as ws:
        log.info("WS connected, subscribing to %d token(s)", len(token_ids))
        await ws.send(subscribe_msg)

        deadline = time.monotonic() + duration
        second_bucket = int(time.monotonic())
        msg_this_second = 0
        total_msgs = 0

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5.0))
            except asyncio.TimeoutError:
                continue

            total_msgs += 1
            msg_this_second += 1

            try:
                parsed = orjson.loads(raw)
            except Exception:
                parsed = raw

            ts = datetime.now(timezone.utc).isoformat()
            line = orjson.dumps({"ts": ts, "msg": parsed}) + b"\n"
            fh.write(line)
            fh.flush()

            # Per-second rate summary
            now_bucket = int(time.monotonic())
            if now_bucket != second_bucket:
                print(f"[t+{now_bucket - (int(time.monotonic()) - duration + duration - int(remaining))}s] "
                      f"{msg_this_second} msg/s  (total {total_msgs})", flush=True)
                second_bucket = now_bucket
                msg_this_second = 0

        log.info("Stream ended. Total messages: %d", total_msgs)


# Tenacity: one retry, 2s backoff — only for transient WS drops, not logic errors.
@retry(stop=stop_after_attempt(2), wait=wait_fixed(2), reraise=True)
async def _stream_with_retry(token_ids: list[str], duration: int, fh: Any) -> None:
    await _stream_inner(token_ids, duration, fh)


async def cmd_watch_stream(args: argparse.Namespace) -> None:
    # Hard cap: prevent runaway sessions that block the terminal for 10+ minutes by accident.
    MAX_DURATION = 600
    duration = min(args.duration, MAX_DURATION)
    if duration != args.duration:
        log.warning("Duration capped at %ds", MAX_DURATION)

    token_ids: list[str] = args.token_ids
    cap_path, fh = _open_capture("watch-stream")
    log.info("Streaming %d token(s) for %ds, capturing to %s", len(token_ids), duration, cap_path)

    try:
        await _stream_with_retry(token_ids, duration, fh)
    except websockets.WebSocketException as exc:
        log.error("WebSocket error (after retry): %s", exc)
        sys.exit(1)
    finally:
        fh.close()

    print(f"\n[Captured to {cap_path}]")


# ---------------------------------------------------------------------------
# Subcommand: find-resolved
# ---------------------------------------------------------------------------

async def cmd_find_resolved(args: argparse.Namespace) -> None:
    cap_path, fh = _open_capture("find-resolved")
    log.info("Finding resolved markets, limit=%d", args.limit)

    params: dict[str, Any] = {
        "closed": "true",
        "order": "endDate",
        "ascending": "false",
        "limit": args.limit,
    }

    async with _new_client() as client:
        status, body = await _get(client, f"{GAMMA_BASE}/markets", fh, params=params)
    fh.close()

    log.info("Status %d", status)

    if not isinstance(body, list):
        _print(body)
        sys.exit(1 if status >= 400 else 0)

    for m in body:
        print(f"\ncondition_id : {m.get('conditionId')}")
        print(f"  slug              : {m.get('slug')}")
        print(f"  endDate           : {m.get('endDate')}")
        print(f"  outcomePrices     : {m.get('outcomePrices')}")
        print(f"  umaResolutionStatus: {m.get('umaResolutionStatus')}")
        print(f"  resolved          : {m.get('resolved')}")
        print(f"  resolutionSource  : {m.get('resolutionSource')}")

    print(f"\n[{len(body)} markets, captured to {cap_path}]")


# ---------------------------------------------------------------------------
# Subcommand: probe-errors
# ---------------------------------------------------------------------------

async def cmd_probe_errors(args: argparse.Namespace) -> None:
    cap_path, fh = _open_capture("probe-errors")
    log.info("Probing error surface, capturing to %s", cap_path)

    # We need one valid condition_id to exercise the extra-segment 404.
    # If we can't fetch one, fall back to a placeholder — the 404 shape is what matters.
    valid_cid_placeholder = "0x" + "a" * 64

    probes: list[tuple[str, str, str | None, dict[str, Any] | None]] = [
        # (label, method, url, post_body)
        ("malformed_condition_id", "GET", f"{CLOB_BASE}/markets/0x", None),
        ("nonexistent_token", "GET", f"{CLOB_BASE}/book", None),  # params added below
        ("extra_path_segment", "GET", f"{CLOB_BASE}/markets/{valid_cid_placeholder}/extra/path/segment", None),
        ("post_order_no_body", "POST", f"{CLOB_BASE}/order", None),
    ]

    summary_rows = []

    async with _new_client() as client:
        for label, method, url, body_payload in probes:
            params = None
            if label == "nonexistent_token":
                params = {"token_id": "999999999999"}

            log.info("Probe: %s %s", method, url)
            if method == "GET":
                status, resp_body = await _get(client, url, fh, params=params)
            else:
                status, resp_body = await _post_raw(client, url, fh, json_body=body_payload)

            # Summarise error shape
            error_keys = list(resp_body.keys()) if isinstance(resp_body, dict) else []
            summary_rows.append({
                "probe": label,
                "status": status,
                "body_type": type(resp_body).__name__,
                "error_keys": error_keys,
                "snippet": str(resp_body)[:120],
            })

    fh.close()

    print("\n=== Probe error summary ===")
    for row in summary_rows:
        print(f"\n  [{row['probe']}]")
        print(f"    status     : {row['status']}")
        print(f"    body_type  : {row['body_type']}")
        print(f"    error_keys : {row['error_keys']}")
        print(f"    snippet    : {row['snippet']}")

    print(f"\n[Captured to {cap_path}]")


# ---------------------------------------------------------------------------
# Subcommand: probe-rate-limit
# ---------------------------------------------------------------------------

async def cmd_probe_rate_limit(args: argparse.Namespace) -> None:
    cap_path, fh = _open_capture("probe-rate-limit")
    total = args.requests
    concurrency = args.concurrency
    log.info("Rate-limit probe: %d requests, concurrency=%d, capturing to %s", total, concurrency, cap_path)

    sem = asyncio.Semaphore(concurrency)
    results: list[dict[str, Any]] = []
    hit_429 = asyncio.Event()

    async def one_request(client: httpx.AsyncClient, n: int) -> None:
        if hit_429.is_set():
            return
        async with sem:
            if hit_429.is_set():
                return
            t0 = time.perf_counter()
            try:
                resp = await client.get(f"{CLOB_BASE}/markets", params={"limit": 1})
            except httpx.RequestError as exc:
                results.append({"n": n, "error": str(exc)})
                return
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)

            try:
                body = orjson.loads(resp.content)
            except Exception:
                body = resp.text

            entry: dict[str, Any] = {
                "n": n,
                "status": resp.status_code,
                "latency_ms": latency_ms,
                "retry_after": resp.headers.get("Retry-After"),
            }
            results.append(entry)

            capture = CapturedRequest(
                url=str(resp.url),
                method="GET",
                request_body=None,
                response_status=resp.status_code,
                response_headers=dict(resp.headers),
                response_body=body,
                latency_ms=latency_ms,
            )
            _write_capture(fh, capture)

            if resp.status_code == 429:
                log.warning("429 hit at request #%d, Retry-After=%s, body=%s",
                            n, resp.headers.get("Retry-After"), str(body)[:200])
                hit_429.set()

    async with _new_client() as client:
        tasks = [asyncio.create_task(one_request(client, i + 1)) for i in range(total)]
        await asyncio.gather(*tasks)

    fh.close()

    sent = len(results)
    errors = [r for r in results if "error" in r]
    four29s = [r for r in results if r.get("status") == 429]
    first_429 = min((r["n"] for r in four29s), default=None)
    retry_after = four29s[0].get("retry_after") if four29s else None

    print(f"\n=== Rate-limit probe summary ===")
    print(f"  Requests sent    : {sent}")
    print(f"  Errors           : {len(errors)}")
    print(f"  429s received    : {len(four29s)}")
    print(f"  First 429 at req : {first_429}")
    print(f"  Retry-After      : {retry_after}")
    if four29s:
        print(f"  429 body snippet : {str(four29s[0].get('body', ''))[:200]}")
    print(f"\n[Captured to {cap_path}]")


# ---------------------------------------------------------------------------
# Argument parsing and dispatch
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket_scout",
        description="Phase 1 Polymarket API scout. All outputs captured to scout/runs/.",
    )
    sub = parser.add_subparsers(dest="subcmd", required=True)

    # list-markets
    p = sub.add_parser("list-markets", help="List markets from Gamma API")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--closed", action="store_true", default=False,
                   help="List closed markets instead of active")

    # get-market
    p = sub.add_parser("get-market", help="Fetch market from CLOB + Gamma; show schema diff")
    p.add_argument("condition_id", help="bytes32 condition id (0x...)")

    # get-orderbook
    p = sub.add_parser("get-orderbook", help="Top-5 orderbook levels + spread + latency")
    p.add_argument("token_id", help="ERC-1155 token/asset id")

    # get-trades
    p = sub.add_parser("get-trades", help="Recent trades for a market (captures auth errors)")
    p.add_argument("condition_id", help="bytes32 condition id")
    p.add_argument("--limit", type=int, default=20)

    # watch-stream
    p = sub.add_parser("watch-stream", help="Subscribe to WS book updates")
    p.add_argument("token_ids", nargs="+", metavar="token_id")
    p.add_argument("--duration", type=int, default=60, help="Seconds to watch (max 600)")

    # find-resolved
    p = sub.add_parser("find-resolved", help="List recently resolved markets")
    p.add_argument("--limit", type=int, default=20)

    # probe-errors
    sub.add_parser("probe-errors", help="Fire bad requests; capture error shapes")

    # probe-rate-limit
    p = sub.add_parser("probe-rate-limit", help="Burst until 429 or exhausted")
    p.add_argument("--requests", type=int, default=200)
    p.add_argument("--concurrency", type=int, default=20)

    return parser


DISPATCH: dict[str, Any] = {
    "list-markets": cmd_list_markets,
    "get-market": cmd_get_market,
    "get-orderbook": cmd_get_orderbook,
    "get-trades": cmd_get_trades,
    "watch-stream": cmd_watch_stream,
    "find-resolved": cmd_find_resolved,
    "probe-errors": cmd_probe_errors,
    "probe-rate-limit": cmd_probe_rate_limit,
}


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    fn = DISPATCH[args.subcmd]
    await fn(args)


if __name__ == "__main__":
    asyncio.run(main())
