"""
Polymarket scout — Phase 1 throwaway CLI.

Probes the Polymarket public CLOB and Gamma APIs and captures raw findings to runs/.
No auth, no retries (except WS reconnect), no abstractions. The outputs are the point.

Usage: python polymarket_scout.py <subcommand> [args]
"""

import argparse
import asyncio
import json
import logging
import os
import re
import stat
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

# Default key file path; overridden by POLYMARKET_SCOUT_KEYFILE env var.
_DEFAULT_KEYFILE = Path.home() / ".polymarket-scout" / "disposable.key"

# USDC.e on Polygon mainnet
_POLYGON_USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Keys whose values must be redacted from captured payloads.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    ["signature", "private_key", "api_secret", "passphrase"]
)

# ---------------------------------------------------------------------------
# Redaction filter — masks any value from env vars matching POLYMARKET_*_KEY
# or *_PRIVATE_*.  Extended at runtime by _extend_redaction_patterns() when
# a key or derived API creds are loaded.
# ---------------------------------------------------------------------------

def _build_redaction_patterns() -> list[re.Pattern[str]]:
    patterns = []
    for key, val in os.environ.items():
        if re.search(r"POLYMARKET_.+_KEY|.+_PRIVATE_.+", key) and val:
            patterns.append(re.compile(re.escape(val)))
    return patterns


_REDACT_PATTERNS: list[re.Pattern[str]] = _build_redaction_patterns()


def _extend_redaction_patterns(*values: str) -> None:
    """Add additional literal string values to the redaction set at runtime.

    Args:
        *values: Plain-text strings (private keys, secrets, etc.) to mask.
    """
    for v in values:
        if v and v not in ("", "***REDACTED***"):
            _REDACT_PATTERNS.append(re.compile(re.escape(v)))


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


def _redact_payload(body: Any) -> Any:
    """Walk a dict/list and redact values for sensitive keys.

    Replaces any value whose key (case-insensitive) is in _SENSITIVE_KEYS with
    the string ``***REDACTED***``.  Non-dict/list leaves are returned unchanged.

    Args:
        body: Arbitrary JSON-serialisable value.

    Returns:
        A copy of body with sensitive fields replaced.
    """
    if isinstance(body, dict):
        return {
            k: "***REDACTED***" if k.lower() in _SENSITIVE_KEYS else _redact_payload(v)
            for k, v in body.items()
        }
    if isinstance(body, list):
        return [_redact_payload(item) for item in body]
    return body


def _write_capture(fh: Any, capture: CapturedRequest) -> None:
    line = orjson.dumps({
        "ts": capture.ts,
        "request": {
            "method": capture.method,
            "url": capture.url,
            "body": _redact_payload(capture.request_body),
        },
        "response_status": capture.response_status,
        "response_headers": capture.response_headers,
        "response_body": _redact_payload(capture.response_body),
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
# Key-file helpers (shared across signing subcommands)
# ---------------------------------------------------------------------------

def _keyfile_path() -> Path:
    """Return the resolved key-file path from env or default.

    Returns:
        Path object for the key file.
    """
    env_val = os.environ.get("POLYMARKET_SCOUT_KEYFILE", "")
    if env_val:
        return Path(env_val).expanduser()
    return _DEFAULT_KEYFILE


def _load_private_key() -> str:
    """Load hex private key from keyfile, enforcing mode 0600.

    Returns:
        Hex private key string (with or without 0x prefix).

    Raises:
        SystemExit: If file missing, wrong permissions, or content invalid.
    """
    kf = _keyfile_path()
    if not kf.exists():
        print(
            f"ERROR: Key file not found: {kf}\n"
            "Run `init-wallet` first, or set POLYMARKET_SCOUT_KEYFILE.",
            file=sys.stderr,
        )
        sys.exit(1)

    file_stat = kf.stat()
    mode = stat.S_IMODE(file_stat.st_mode)
    if mode != 0o600:
        print(
            f"ERROR: Key file {kf} has permissions {oct(mode)}, expected 0600.\n"
            "Fix with: chmod 600 " + str(kf),
            file=sys.stderr,
        )
        sys.exit(1)

    raw = kf.read_text().strip()
    if not raw:
        print(f"ERROR: Key file {kf} is empty.", file=sys.stderr)
        sys.exit(1)

    return raw


def _apikeys_path() -> Path:
    """Return path for storing derived API keys (sibling of keyfile).

    Returns:
        Path object for the .apikeys.json file.
    """
    kf = _keyfile_path()
    stem = kf.stem  # e.g. "disposable" from "disposable.key"
    return kf.parent / f"{stem}.apikeys.json"


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
# Subcommand: init-wallet
# ---------------------------------------------------------------------------

def cmd_init_wallet(args: argparse.Namespace) -> None:
    """Generate a fresh disposable EOA and write the private key to keyfile.

    Refuses to overwrite an existing keyfile unless ``--force`` is passed.
    Prints the wallet address plus a safety banner.

    Args:
        args: Parsed arguments.  Reads ``args.force``.
    """
    from eth_account import Account  # noqa: PLC0415

    kf = _keyfile_path()

    if kf.exists() and not args.force:
        print(
            f"ERROR: Key file already exists: {kf}\n"
            "Use --force to overwrite (existing key will be lost!).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Ensure parent directory exists with mode 0700.
    kf.parent.mkdir(parents=True, exist_ok=True)
    kf.parent.chmod(0o700)

    account = Account.create()
    private_key_hex: str = account.key.hex()  # "0x..." 32-byte hex

    # Write key file with mode 0600 (owner read/write only).
    kf.touch(mode=0o600, exist_ok=True)
    kf.write_text(private_key_hex)
    kf.chmod(0o600)

    address: str = account.address

    print(f"\nWallet address : {address}")
    print(f"Key file       : {kf}")
    print()
    print("=" * 72)
    print("  DISPOSABLE WALLET — READ CAREFULLY")
    print("=" * 72)
    print("  * This key is for Phase 1 scouting ONLY.")
    print("  * Hard cap: deposit no more than $10 of MATIC/USDC on mainnet.")
    print("  * NEVER reuse this key for any production bot or wallet.")
    print("  * NEVER commit this key to git, paste it in Slack, or share it.")
    print("  * The key file is stored at:", kf)
    print("=" * 72)


# ---------------------------------------------------------------------------
# Subcommand: wallet-info
# ---------------------------------------------------------------------------

async def cmd_wallet_info(args: argparse.Namespace) -> None:
    """Load keyfile and print address.  Optionally fetches MATIC + USDC balance.

    Args:
        args: Parsed arguments.  Reads ``args.rpc`` and ``args.usdc``.
    """
    from eth_account import Account  # noqa: PLC0415

    private_key = _load_private_key()
    # Register key in redaction so it never leaks into logs.
    _extend_redaction_patterns(private_key, private_key.lstrip("0x"))

    account = Account.from_key(private_key)
    address: str = account.address

    print(f"Wallet address : {address}")
    print(f"Key file       : {_keyfile_path()}")

    rpc_url: str | None = args.rpc
    if not rpc_url:
        print("(Pass --rpc <url> to fetch on-chain balances)")
        return

    # Lazy import web3 only when --rpc is provided.
    from web3 import Web3  # noqa: PLC0415

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        print(f"ERROR: Cannot connect to RPC at {rpc_url}", file=sys.stderr)
        sys.exit(1)

    matic_wei = w3.eth.get_balance(address)
    matic = matic_wei / 10**18
    print(f"MATIC balance  : {matic:.6f}")

    usdc_address: str = args.usdc or _POLYGON_USDC_ADDRESS
    # Minimal ERC-20 ABI: just balanceOf.
    erc20_abi: list[dict[str, Any]] = [
        {
            "inputs": [{"name": "account", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        }
    ]
    usdc_contract = w3.eth.contract(
        address=Web3.to_checksum_address(usdc_address), abi=erc20_abi
    )
    usdc_raw: int = usdc_contract.functions.balanceOf(address).call()
    # USDC.e on Polygon has 6 decimals.
    usdc_balance = usdc_raw / 10**6
    print(f"USDC balance   : {usdc_balance:.6f}  (contract: {usdc_address})")


# ---------------------------------------------------------------------------
# Subcommand: derive-api-keys
# ---------------------------------------------------------------------------

def cmd_derive_api_keys(args: argparse.Namespace) -> None:
    """Derive (or create) L2 API credentials via py-clob-client.

    Stores results at ``<keyfile-stem>.apikeys.json`` (mode 0600).
    Prints only the first 8 chars of api_key plus the wallet address.

    Args:
        args: Parsed arguments.  Reads ``args.host`` and ``args.chain_id``.
    """
    from py_clob_client.client import ClobClient  # noqa: PLC0415
    from py_clob_client.exceptions import PolyException  # noqa: PLC0415

    private_key = _load_private_key()
    _extend_redaction_patterns(private_key, private_key.lstrip("0x"))

    host: str = args.host
    chain_id: int = args.chain_id

    log.info("Connecting to CLOB host=%s chain_id=%d", host, chain_id)

    try:
        # Level-1 client: key only, no creds yet.
        client = ClobClient(host=host, chain_id=chain_id, key=private_key)
        address = client.get_address()
        # Extend redaction with the wallet address (treat as PII).
        _extend_redaction_patterns(address, address.lower() if address else "")

        log.info("Deriving API keys for address=%s", address)
        creds = client.create_or_derive_api_creds()
    except PolyException as exc:
        print(f"ERROR: CLOB returned an error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Unexpected error during key derivation: {exc}", file=sys.stderr)
        sys.exit(1)

    if creds is None:
        print("ERROR: Received null credentials from CLOB.", file=sys.stderr)
        sys.exit(1)

    # Extend redaction with the derived L2 secret material.
    _extend_redaction_patterns(creds.api_secret, creds.api_passphrase, creds.api_key)

    # Persist to disk, mode 0600.
    out_path = _apikeys_path()
    payload = {
        "api_key": creds.api_key,
        "api_secret": creds.api_secret,
        "api_passphrase": creds.api_passphrase,
        "address": address,
        "host": host,
        "chain_id": chain_id,
    }
    out_path.touch(mode=0o600, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    out_path.chmod(0o600)

    key_prefix = creds.api_key[:8] if creds.api_key else "?"
    print(f"API key prefix : {key_prefix}...")
    print(f"Wallet address : {address}")
    print(f"Keys stored at : {out_path}")
    log.info("API credentials saved (redacted from logs)")


# ---------------------------------------------------------------------------
# Internal: load stored API creds (used by place-resting-order, cancel-order, etc.)
# ---------------------------------------------------------------------------

def _load_api_creds() -> tuple[str, str, str, str, str]:
    """Load private key and stored API creds from disk.

    Returns:
        Tuple of (private_key, api_key, api_secret, api_passphrase, address).

    Raises:
        SystemExit: If key file or apikeys file is missing/unreadable.
    """
    from py_clob_client.clob_types import ApiCreds  # noqa: PLC0415

    private_key = _load_private_key()
    _extend_redaction_patterns(private_key, private_key.lstrip("0x"))

    ap = _apikeys_path()
    if not ap.exists():
        print(
            f"ERROR: API keys file not found: {ap}\n"
            "Run `derive-api-keys` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    raw = json.loads(ap.read_text())
    api_key: str = raw["api_key"]
    api_secret: str = raw["api_secret"]
    api_passphrase: str = raw["api_passphrase"]
    address: str = raw.get("address", "")

    _extend_redaction_patterns(api_key, api_secret, api_passphrase, address, address.lower())
    return private_key, api_key, api_secret, api_passphrase, address


def _build_level2_client(
    private_key: str,
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    host: str,
    chain_id: int,
) -> Any:
    """Construct a Level-2 ClobClient.

    Args:
        private_key: Hex private key.
        api_key: CLOB API key.
        api_secret: CLOB API secret.
        api_passphrase: CLOB API passphrase.
        host: CLOB host URL.
        chain_id: Polygon chain ID (137 mainnet, 80002 Amoy).

    Returns:
        Configured ClobClient instance.
    """
    from py_clob_client.client import ClobClient  # noqa: PLC0415
    from py_clob_client.clob_types import ApiCreds  # noqa: PLC0415

    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )
    return ClobClient(
        host=host,
        chain_id=chain_id,
        key=private_key,
        creds=creds,
    )


# ---------------------------------------------------------------------------
# Subcommand: place-resting-order
# ---------------------------------------------------------------------------

def cmd_place_resting_order(args: argparse.Namespace) -> None:
    """Place a limit order well off the touch; captures EIP-712 struct + response.

    Safety guards:
    - Hard cap: price * size <= 5 USDC notional.
    - BUY price <= 0.20; SELL price >= 0.80 (override with --allow-near-touch).
    - Price must be at least N ticks away from the current best; rejects if not.

    Args:
        args: Parsed arguments.
    """
    from py_clob_client.client import ClobClient  # noqa: PLC0415
    from py_clob_client.clob_types import OrderArgs  # noqa: PLC0415
    from py_clob_client.exceptions import PolyException  # noqa: PLC0415

    private_key, api_key, api_secret, api_passphrase, address = _load_api_creds()

    token_id: str = args.token_id
    side: str = args.side.upper()
    price: float = float(args.price)
    size: float = float(args.size)
    host: str = args.host
    chain_id: int = args.chain_id

    # Hard notional cap: 5 USDC.
    notional = price * size
    if notional > 5.0:
        print(
            f"ERROR: Notional {notional:.4f} USDC exceeds hard cap of 5 USDC.\n"
            f"  price={price}  size={size}  price*size={notional:.4f}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Safety price checks.
    if side == "BUY" and price > 0.20 and not args.allow_near_touch:
        print(
            f"WARNING: BUY price {price} is above the safe default of 0.20.\n"
            "Pass --allow-near-touch to override (order may fill!).",
            file=sys.stderr,
        )
        sys.exit(1)
    if side == "SELL" and price < 0.80 and not args.allow_near_touch:
        print(
            f"WARNING: SELL price {price} is below the safe default of 0.80.\n"
            "Pass --allow-near-touch to override (order may fill!).",
            file=sys.stderr,
        )
        sys.exit(1)

    client = _build_level2_client(private_key, api_key, api_secret, api_passphrase, host, chain_id)

    # Fetch current best to verify distance from touch.
    try:
        tick_size_str: str = client.get_tick_size(token_id)
        tick_size = float(tick_size_str)
    except Exception as exc:
        print(f"ERROR: Could not fetch tick size: {exc}", file=sys.stderr)
        sys.exit(1)

    # We require the order to be at least 5 ticks away from touch (configurable floor).
    _MIN_TICKS_FROM_TOUCH = 5
    try:
        book = client.get_order_book(token_id)
    except Exception as exc:
        print(f"WARNING: Could not fetch orderbook to validate distance from touch: {exc}",
              file=sys.stderr)
        book = None

    if book is not None:
        if side == "BUY" and book.asks:
            best_ask = float(book.asks[0].price)
            distance = best_ask - price
            if distance < _MIN_TICKS_FROM_TOUCH * tick_size:
                print(
                    f"ERROR: BUY price {price} is only {distance:.4f} from best ask {best_ask} "
                    f"(need >= {_MIN_TICKS_FROM_TOUCH * tick_size:.4f} = {_MIN_TICKS_FROM_TOUCH} ticks).\n"
                    "Increase distance or use --allow-near-touch.",
                    file=sys.stderr,
                )
                sys.exit(1)
        elif side == "SELL" and book.bids:
            best_bid = float(book.bids[0].price)
            distance = price - best_bid
            if distance < _MIN_TICKS_FROM_TOUCH * tick_size:
                print(
                    f"ERROR: SELL price {price} is only {distance:.4f} from best bid {best_bid} "
                    f"(need >= {_MIN_TICKS_FROM_TOUCH * tick_size:.4f} = {_MIN_TICKS_FROM_TOUCH} ticks).\n"
                    "Increase distance or use --allow-near-touch.",
                    file=sys.stderr,
                )
                sys.exit(1)

    cap_path, fh = _open_capture("place-resting-order")
    log.info(
        "Placing resting %s order: token_id=%s price=%s size=%s notional=%.4f, capture=%s",
        side, token_id, price, size, notional, cap_path,
    )

    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=side,
    )

    try:
        # create_order returns a SignedOrder; capture its dict() before submitting.
        signed_order = client.create_order(order_args)
    except PolyException as exc:
        print(f"ERROR: Order creation failed: {exc}", file=sys.stderr)
        fh.close()
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Unexpected error building order: {exc}", file=sys.stderr)
        fh.close()
        sys.exit(1)

    # Capture the full constructed EIP-712 order dict BEFORE signing field is exposed.
    # signed_order.order.dict() has the struct fields; signed_order.dict() adds signature.
    raw_order_struct: dict[str, Any] = signed_order.order.dict()
    # signed_order.dict() includes the signature — redact it via _redact_payload.
    order_with_sig: dict[str, Any] = signed_order.dict()

    pre_sign_capture: dict[str, Any] = {
        "event": "pre_submit_order_struct",
        "note": "EIP-712 typed-data message fields (signature redacted)",
        "order_struct": raw_order_struct,  # no signature field here
        "signed_order_redacted": _redact_payload(order_with_sig),
    }
    pre_sign_line = orjson.dumps(pre_sign_capture) + b"\n"
    fh.write(pre_sign_line)
    fh.flush()

    # Post the order via the SDK (which uses its own requests session internally).
    # We capture what we can; the SDK does the HTTP itself so we reconstruct a capture entry.
    from py_clob_client.constants import OrderType  # noqa: PLC0415

    t0 = time.perf_counter()
    try:
        result = client.post_order(signed_order)
    except PolyException as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        error_body: dict[str, Any] = {"error": str(exc)}
        # Attempt to extract underlying HTTP body from PolyException.
        if hasattr(exc, "args") and exc.args:
            try:
                error_body = json.loads(exc.args[0]) if isinstance(exc.args[0], str) else {"error": str(exc)}
            except Exception:
                error_body = {"error": str(exc)}
        capture = CapturedRequest(
            url=f"{host}/order",
            method="POST",
            request_body=_redact_payload(order_with_sig),
            response_status=400,
            response_headers={},
            response_body=_redact_payload(error_body),
            latency_ms=latency_ms,
        )
        _write_capture(fh, capture)
        fh.close()
        print(f"ERROR: Post order failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        capture = CapturedRequest(
            url=f"{host}/order",
            method="POST",
            request_body=_redact_payload(order_with_sig),
            response_status=0,
            response_headers={},
            response_body={"error": str(exc)},
            latency_ms=latency_ms,
        )
        _write_capture(fh, capture)
        fh.close()
        print(f"ERROR: Unexpected error posting order: {exc}", file=sys.stderr)
        sys.exit(1)

    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    capture = CapturedRequest(
        url=f"{host}/order",
        method="POST",
        request_body=_redact_payload(order_with_sig),
        response_status=200,
        response_headers={},
        response_body=_redact_payload(result) if isinstance(result, dict) else str(result),
        latency_ms=latency_ms,
    )
    _write_capture(fh, capture)
    fh.close()

    order_id: str = result.get("orderID", "") if isinstance(result, dict) else ""
    status_val: str = result.get("status", "") if isinstance(result, dict) else str(result)

    print(f"order_id : {order_id}")
    print(f"status   : {status_val}")
    print(f"\n[Captured to {cap_path}]")

    if not order_id:
        log.warning("No order_id in response; check capture for details.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: cancel-order
# ---------------------------------------------------------------------------

def cmd_cancel_order(args: argparse.Namespace) -> None:
    """Cancel an order by ID.  Sends the cancel twice to observe idempotency.

    Args:
        args: Parsed arguments.  Reads ``args.order_id``, ``args.host``,
              ``args.chain_id``.
    """
    from py_clob_client.exceptions import PolyException  # noqa: PLC0415

    private_key, api_key, api_secret, api_passphrase, address = _load_api_creds()
    order_id: str = args.order_id
    host: str = args.host
    chain_id: int = args.chain_id

    client = _build_level2_client(private_key, api_key, api_secret, api_passphrase, host, chain_id)

    cap_path, fh = _open_capture("cancel-order")
    log.info("Cancelling order_id=%s, capturing to %s", order_id, cap_path)

    for attempt in (1, 2):
        log.info("Cancel attempt %d for order_id=%s", attempt, order_id)
        t0 = time.perf_counter()
        try:
            result = client.cancel(order_id)
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            status_code = 200
            resp_body = result if isinstance(result, dict) else {"result": str(result)}
        except PolyException as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            status_code = 400
            resp_body = {"error": str(exc)}
        except Exception as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            status_code = 0
            resp_body = {"error": str(exc)}

        capture = CapturedRequest(
            url=f"{host}/order",
            method="DELETE",
            request_body={"orderID": order_id, "attempt": attempt},
            response_status=status_code,
            response_headers={},
            response_body=_redact_payload(resp_body),
            latency_ms=latency_ms,
        )
        _write_capture(fh, capture)

        print(f"\n--- Cancel attempt {attempt} ---")
        print(f"  status_code : {status_code}")
        print(f"  latency_ms  : {latency_ms}")
        print(f"  response    : {resp_body}")

    fh.close()
    print(f"\n[Captured to {cap_path}]")
    print("(Two cancel attempts captured — compare responses to observe idempotency behaviour.)")


# ---------------------------------------------------------------------------
# Subcommand: replay-same-nonce
# ---------------------------------------------------------------------------

def cmd_replay_same_nonce(args: argparse.Namespace) -> None:
    """Reconstruct an order with the same salt/nonce and submit twice.

    Either retrieves original order parameters from the CLOB (if ``--salt``
    is not given) or uses ``--salt`` directly.  The goal is to observe whether
    Polymarket deduplicates on the salt/signature or accepts two submissions.

    Args:
        args: Parsed arguments.  Reads ``args.order_id``, ``args.salt``,
              ``args.host``, ``args.chain_id``.
    """
    from py_clob_client.clob_types import OrderArgs  # noqa: PLC0415
    from py_clob_client.exceptions import PolyException  # noqa: PLC0415
    from py_order_utils.builders.order_builder import OrderBuilder as UtilsOrderBuilder  # noqa: PLC0415
    from py_order_utils.model import OrderData, EOA  # noqa: PLC0415
    from py_order_utils.signer import Signer as UtilsSigner  # noqa: PLC0415
    from py_clob_client.config import get_contract_config  # noqa: PLC0415

    private_key, api_key, api_secret, api_passphrase, address = _load_api_creds()
    order_id: str = args.order_id
    host: str = args.host
    chain_id: int = args.chain_id

    client = _build_level2_client(private_key, api_key, api_secret, api_passphrase, host, chain_id)

    cap_path, fh = _open_capture("replay-same-nonce")
    log.info("Replaying same-nonce for order_id=%s, capturing to %s", order_id, cap_path)

    # Attempt to fetch the original order.
    original: dict[str, Any] | None = None
    if not args.salt:
        try:
            original = client.get_order(order_id)
            log.info("Retrieved original order from CLOB")
        except PolyException as exc:
            log.warning("Could not retrieve order %s from CLOB: %s", order_id, exc)
        except Exception as exc:
            log.warning("Unexpected error fetching order: %s", exc)

    salt: int
    if args.salt:
        salt = int(args.salt)
    elif original and isinstance(original, dict):
        # The CLOB returns the order JSON which includes the salt from the EIP-712 struct.
        salt_raw = original.get("salt", original.get("order", {}).get("salt", None))
        if salt_raw is None:
            print(
                "ERROR: Could not extract salt from original order. Use --salt <int>.",
                file=sys.stderr,
            )
            fh.close()
            sys.exit(1)
        salt = int(salt_raw)
    else:
        print(
            "ERROR: Cannot replay — could not fetch original order and --salt not provided.",
            file=sys.stderr,
        )
        fh.close()
        sys.exit(1)

    # Extract parameters from original order if available.
    token_id: str
    price: float
    size: float
    side: str
    if original and isinstance(original, dict):
        # CLOB returns fields like tokenId/token_id, size, price, side.
        token_id = str(original.get("asset_id") or original.get("tokenId", ""))
        price = float(original.get("price", 0.05))
        size = float(original.get("original_size") or original.get("size", 1.0))
        side_raw = str(original.get("side", "BUY")).upper()
        side = side_raw
    else:
        print(
            "ERROR: No original order data available; cannot reconstruct parameters.",
            file=sys.stderr,
        )
        fh.close()
        sys.exit(1)

    log.info("Replaying: token_id=%s side=%s price=%s size=%s salt=%d", token_id, side, price, size, salt)

    # Determine tick size and neg_risk for the token.
    try:
        tick_size_str: str = client.get_tick_size(token_id)
        neg_risk: bool = client.get_neg_risk(token_id)
    except Exception as exc:
        print(f"ERROR: Could not fetch market metadata: {exc}", file=sys.stderr)
        fh.close()
        sys.exit(1)

    from py_clob_client.clob_types import CreateOrderOptions  # noqa: PLC0415
    from py_clob_client.order_builder.builder import OrderBuilder, ROUNDING_CONFIG  # noqa: PLC0415
    from py_clob_client.order_builder.constants import BUY, SELL  # noqa: PLC0415

    # Build two identical orders with the same salt by overriding salt_generator.
    def _fixed_salt_generator() -> int:
        return salt

    contract_config = get_contract_config(chain_id, neg_risk)
    utils_builder = UtilsOrderBuilder(
        exchange_address=contract_config.exchange,
        chain_id=chain_id,
        signer=UtilsSigner(key=private_key),
        salt_generator=_fixed_salt_generator,
    )

    from py_order_utils.model import BUY as UtilsBuy, SELL as UtilsSell  # noqa: PLC0415
    from py_clob_client.order_builder.helpers import to_token_decimals, round_normal, round_down  # noqa: PLC0415

    round_cfg = ROUNDING_CONFIG[tick_size_str]
    raw_price = round_normal(price, round_cfg.price)

    if side == "BUY":
        raw_taker = round_down(size, round_cfg.size)
        raw_maker = raw_taker * raw_price
        side_val = UtilsBuy
        maker_amount = to_token_decimals(raw_maker)
        taker_amount = to_token_decimals(raw_taker)
    else:
        raw_maker = round_down(size, round_cfg.size)
        raw_taker = raw_maker * raw_price
        side_val = UtilsSell
        maker_amount = to_token_decimals(raw_maker)
        taker_amount = to_token_decimals(raw_taker)

    from py_order_utils.model import OrderData  # noqa: PLC0415
    from py_order_utils.model.signatures import EOA  # noqa: PLC0415

    order_data = OrderData(
        maker=address,
        taker="0x0000000000000000000000000000000000000000",
        tokenId=token_id,
        makerAmount=str(maker_amount),
        takerAmount=str(taker_amount),
        side=side_val,
        feeRateBps="0",
        nonce="0",
        signer=address,
        expiration="0",
        signatureType=EOA,
    )

    from py_clob_client.utilities import order_to_json  # noqa: PLC0415
    from py_clob_client.constants import OrderType  # noqa: PLC0415

    for attempt in (1, 2):
        log.info("Submitting same-nonce attempt %d", attempt)
        signed = utils_builder.build_signed_order(order_data)
        body_dict = order_to_json(signed, api_key, OrderType.GTC)

        t0 = time.perf_counter()
        try:
            result = client.post_order(signed)
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            status_code = 200
            resp_body = result if isinstance(result, dict) else {"result": str(result)}
        except PolyException as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            status_code = 400
            resp_body = {"error": str(exc)}
        except Exception as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            status_code = 0
            resp_body = {"error": str(exc)}

        capture = CapturedRequest(
            url=f"{host}/order",
            method="POST",
            request_body=_redact_payload(body_dict),
            response_status=status_code,
            response_headers={},
            response_body=_redact_payload(resp_body),
            latency_ms=latency_ms,
        )
        _write_capture(fh, capture)

        print(f"\n--- Replay attempt {attempt} (salt={salt}) ---")
        print(f"  status_code : {status_code}")
        print(f"  latency_ms  : {latency_ms}")
        print(f"  response    : {resp_body}")

    fh.close()
    print(f"\n[Captured to {cap_path}]")
    print("(Compare attempt 1 vs 2: does Polymarket reject the duplicate salt?)")


# ---------------------------------------------------------------------------
# Subcommand: bad-orders
# ---------------------------------------------------------------------------

def cmd_bad_orders(args: argparse.Namespace) -> None:
    """Fire deliberately invalid signed orders and capture error shapes.

    Probes:
    1. Price not a multiple of minimum_tick_size.
    2. Size below minimum_order_size.
    3. Order signed by EOA with zero USDC allowance to exchange contract.
    4. expiration in the past.
    5. Wrong signature_type (EOA=0 used even for a Polymarket-proxy wallet).

    Args:
        args: Parsed arguments.  Reads ``args.token_id``, ``args.host``,
              ``args.chain_id``.
    """
    from py_clob_client.clob_types import OrderArgs, CreateOrderOptions  # noqa: PLC0415
    from py_clob_client.exceptions import PolyException  # noqa: PLC0415
    from py_clob_client.constants import OrderType  # noqa: PLC0415
    from py_clob_client.utilities import order_to_json  # noqa: PLC0415
    from py_order_utils.model.signatures import EOA, POLY_PROXY  # noqa: PLC0415

    private_key, api_key, api_secret, api_passphrase, address = _load_api_creds()
    token_id: str = args.token_id
    host: str = args.host
    chain_id: int = args.chain_id

    client = _build_level2_client(private_key, api_key, api_secret, api_passphrase, host, chain_id)

    # Fetch market metadata for constructing orders.
    try:
        tick_size_str: str = client.get_tick_size(token_id)
        neg_risk: bool = client.get_neg_risk(token_id)
    except Exception as exc:
        print(f"ERROR: Could not fetch market metadata for token_id={token_id}: {exc}",
              file=sys.stderr)
        sys.exit(1)

    tick_size = float(tick_size_str)

    cap_path, fh = _open_capture("bad-orders")
    log.info("Firing bad orders for token_id=%s, capturing to %s", token_id, cap_path)

    summary_rows: list[dict[str, Any]] = []

    def _submit_order_args(
        label: str,
        order_args: OrderArgs,
        options: CreateOrderOptions | None = None,
        sig_type_override: int | None = None,
    ) -> None:
        """Build and submit one bad order; capture and add to summary.

        Args:
            label: Human-readable probe label.
            order_args: Order parameters.
            options: Optional override for CreateOrderOptions.
            sig_type_override: If set, override the signatureType in the built order.
        """
        from py_clob_client.order_builder.builder import OrderBuilder  # noqa: PLC0415
        from py_order_utils.builders.order_builder import OrderBuilder as UtilsOrderBuilder  # noqa: PLC0415
        from py_order_utils.signer import Signer as UtilsSigner  # noqa: PLC0415
        from py_clob_client.config import get_contract_config  # noqa: PLC0415
        from py_order_utils.model import OrderData, BUY as UtilsBuy, SELL as UtilsSell  # noqa: PLC0415
        from py_clob_client.order_builder.helpers import to_token_decimals, round_normal, round_down  # noqa: PLC0415
        from py_clob_client.order_builder.builder import ROUNDING_CONFIG  # noqa: PLC0415
        from py_clob_client.order_builder.constants import BUY, SELL  # noqa: PLC0415
        from py_order_utils.utils import generate_seed  # noqa: PLC0415

        # Resolve tick size + neg_risk from options or defaults.
        resolved_tick: str = options.tick_size if options else tick_size_str
        resolved_neg_risk: bool = options.neg_risk if options else neg_risk

        round_cfg = ROUNDING_CONFIG[resolved_tick]
        raw_price = round_normal(order_args.price, round_cfg.price)

        side_str = order_args.side.upper()
        if side_str == BUY:
            raw_taker = round_down(order_args.size, round_cfg.size)
            raw_maker = raw_taker * raw_price
            side_val = UtilsBuy
        else:
            raw_maker = round_down(order_args.size, round_cfg.size)
            raw_taker = raw_maker * raw_price
            side_val = UtilsSell

        maker_amount = to_token_decimals(raw_maker)
        taker_amount = to_token_decimals(raw_taker)

        # Handle sig_type override for "wrong signature_type" probe.
        sig_type = sig_type_override if sig_type_override is not None else EOA

        contract_config = get_contract_config(chain_id, resolved_neg_risk)
        order_data = OrderData(
            maker=address,
            taker="0x0000000000000000000000000000000000000000",
            tokenId=order_args.token_id,
            makerAmount=str(maker_amount),
            takerAmount=str(taker_amount),
            side=side_val,
            feeRateBps=str(order_args.fee_rate_bps),
            nonce=str(order_args.nonce),
            signer=address,
            expiration=str(order_args.expiration),
            signatureType=sig_type,
        )

        utils_builder = UtilsOrderBuilder(
            exchange_address=contract_config.exchange,
            chain_id=chain_id,
            signer=UtilsSigner(key=private_key),
        )

        try:
            signed = utils_builder.build_signed_order(order_data)
        except Exception as exc:
            summary_rows.append({
                "probe": label,
                "build_error": str(exc),
                "status": "BUILD_FAILED",
                "snippet": str(exc)[:120],
            })
            return

        body_dict = order_to_json(signed, api_key, OrderType.GTC)

        t0 = time.perf_counter()
        try:
            result = client.post_order(signed)
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            status_code = 200
            resp_body = result if isinstance(result, dict) else {"result": str(result)}
        except PolyException as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            status_code = 400
            resp_body = {"error": str(exc)}
        except Exception as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            status_code = 0
            resp_body = {"error": str(exc)}

        capture = CapturedRequest(
            url=f"{host}/order",
            method="POST",
            request_body=_redact_payload(body_dict),
            response_status=status_code,
            response_headers={},
            response_body=_redact_payload(resp_body),
            latency_ms=latency_ms,
        )
        _write_capture(fh, capture)

        snippet = str(resp_body)[:120]
        summary_rows.append({
            "probe": label,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "snippet": snippet,
        })

    # 1. Price not a multiple of minimum_tick_size.
    # Use a price that deliberately is not on-grid (offset by half a tick).
    bad_price = round(0.05 + tick_size * 0.5, 8)
    _submit_order_args(
        "price_bad_tick",
        OrderArgs(token_id=token_id, price=bad_price, size=2.0, side="BUY"),
    )

    # 2. Size below minimum_order_size (use 0.000001 — almost certainly below any minimum).
    _submit_order_args(
        "size_below_minimum",
        OrderArgs(token_id=token_id, price=tick_size, size=0.000001, side="BUY"),
    )

    # 3. EOA with zero USDC allowance — we intentionally do NOT approve the exchange contract.
    # For a fresh wallet this is the natural state; just place a valid-looking order.
    _submit_order_args(
        "zero_usdc_allowance",
        OrderArgs(token_id=token_id, price=tick_size, size=1.0, side="BUY"),
    )

    # 4. expiration in the past (Unix epoch 1 = 1970-01-01 00:00:01 UTC).
    _submit_order_args(
        "expired_order",
        OrderArgs(token_id=token_id, price=tick_size, size=1.0, side="BUY", expiration=1),
    )

    # 5. Wrong signature_type: POLY_PROXY=2 when wallet is a plain EOA.
    _submit_order_args(
        "wrong_signature_type",
        OrderArgs(token_id=token_id, price=tick_size, size=1.0, side="BUY"),
        sig_type_override=POLY_PROXY,
    )

    fh.close()

    print("\n=== Bad-orders probe summary ===")
    cols = ["probe", "status_code", "latency_ms", "snippet"]
    for row in summary_rows:
        print(f"\n  [{row['probe']}]")
        for col in cols:
            if col in row:
                print(f"    {col:<14}: {row[col]}")
        if "build_error" in row:
            print(f"    build_error   : {row['build_error']}")
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

    # ---- Auth/signing subcommands ----

    # init-wallet
    p = sub.add_parser(
        "init-wallet",
        help="Generate a fresh disposable EOA; write key to POLYMARKET_SCOUT_KEYFILE",
    )
    p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Overwrite existing keyfile (existing key will be lost)",
    )

    # wallet-info
    p = sub.add_parser(
        "wallet-info",
        help="Print wallet address; optionally fetch MATIC + USDC balance via RPC",
    )
    p.add_argument("--rpc", default=None, metavar="URL",
                   help="Polygon RPC URL (e.g. https://polygon-rpc.com)")
    p.add_argument("--usdc", default=None, metavar="0x...",
                   help=f"USDC contract address override (default: {_POLYGON_USDC_ADDRESS})")

    # derive-api-keys
    p = sub.add_parser(
        "derive-api-keys",
        help="Derive/create L2 API credentials from CLOB; store at <keyfile-stem>.apikeys.json",
    )
    p.add_argument("--host", default=CLOB_BASE, help=f"CLOB host (default: {CLOB_BASE})")
    p.add_argument("--chain-id", type=int, default=137,
                   help="Chain ID: 137=Polygon mainnet, 80002=Amoy testnet (default: 137)")

    # place-resting-order
    p = sub.add_parser(
        "place-resting-order",
        help="Place a limit order well off the touch (hard cap: 5 USDC notional)",
    )
    p.add_argument("--token-id", required=True, help="ERC-1155 token/asset id")
    p.add_argument("--side", required=True, choices=["BUY", "SELL"],
                   help="Order side")
    p.add_argument("--price", required=True, type=float,
                   help="Limit price (0 < price < 1)")
    p.add_argument("--size", required=True, type=float,
                   help="Size in outcome tokens; price*size must be <= 5 USDC")
    p.add_argument("--allow-near-touch", action="store_true", default=False,
                   help="Skip the safety price check (BUY<=0.20, SELL>=0.80 guard)")
    p.add_argument("--host", default=CLOB_BASE, help=f"CLOB host (default: {CLOB_BASE})")
    p.add_argument("--chain-id", type=int, default=137,
                   help="Chain ID: 137=Polygon mainnet, 80002=Amoy testnet (default: 137)")

    # cancel-order
    p = sub.add_parser(
        "cancel-order",
        help="Cancel an order by ID; sends the cancel twice to observe idempotency",
    )
    p.add_argument("order_id", help="Order ID returned by place-resting-order")
    p.add_argument("--host", default=CLOB_BASE, help=f"CLOB host (default: {CLOB_BASE})")
    p.add_argument("--chain-id", type=int, default=137,
                   help="Chain ID: 137=Polygon mainnet, 80002=Amoy testnet (default: 137)")

    # replay-same-nonce
    p = sub.add_parser(
        "replay-same-nonce",
        help="Submit the same signed order twice to test deduplication by salt",
    )
    p.add_argument("order_id", help="Original order ID (used to fetch params from CLOB)")
    p.add_argument("--salt", type=int, default=None,
                   help="Explicit salt override (required if CLOB no longer has the order)")
    p.add_argument("--host", default=CLOB_BASE, help=f"CLOB host (default: {CLOB_BASE})")
    p.add_argument("--chain-id", type=int, default=137,
                   help="Chain ID: 137=Polygon mainnet, 80002=Amoy testnet (default: 137)")

    # bad-orders
    p = sub.add_parser(
        "bad-orders",
        help="Fire deliberately invalid signed orders; capture error shapes",
    )
    p.add_argument("--token-id", required=True, help="ERC-1155 token/asset id to use for probes")
    p.add_argument("--host", default=CLOB_BASE, help=f"CLOB host (default: {CLOB_BASE})")
    p.add_argument("--chain-id", type=int, default=137,
                   help="Chain ID: 137=Polygon mainnet, 80002=Amoy testnet (default: 137)")

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
    # Auth/signing subcommands (sync — no asyncio wrapper needed).
    "init-wallet": cmd_init_wallet,
    "derive-api-keys": cmd_derive_api_keys,
    "cancel-order": cmd_cancel_order,
    "replay-same-nonce": cmd_replay_same_nonce,
    "bad-orders": cmd_bad_orders,
    # wallet-info and place-resting-order are async.
    "wallet-info": cmd_wallet_info,
    "place-resting-order": cmd_place_resting_order,
}

# Subcommands that are plain sync functions (not coroutines).
_SYNC_COMMANDS: frozenset[str] = frozenset(
    ["init-wallet", "derive-api-keys", "cancel-order", "replay-same-nonce", "bad-orders"]
)


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    fn = DISPATCH[args.subcmd]
    if args.subcmd in _SYNC_COMMANDS:
        fn(args)
    else:
        await fn(args)


if __name__ == "__main__":
    asyncio.run(main())
