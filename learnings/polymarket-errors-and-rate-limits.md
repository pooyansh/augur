---
title: Polymarket errors and rate limits — observed shapes
topic: polymarket
observed_on: 2026-05-08
phase: 1
status: current
---

# Polymarket errors and rate limits

## What I observed

`scout/polymarket_scout.py probe-errors` and `probe-rate-limit` against `clob.polymarket.com` from a US/dev IP, no auth.

### Error shapes are **not** uniform

| Probe | Status | Body type | Example body |
|---|---|---|---|
| `GET /markets/0xINVALID` | 404 | JSON | `{"error": "market not found"}` |
| `GET /book?token_id=999999999` | 404 | JSON | `{"error": "No orderbook exists for the requested token id"}` |
| `GET /markets/<valid>/extra/path/segment` | 404 | **plain text** | `404 page not found` |
| `POST /order` (no body) | 401 | JSON | `{"error": "Unauthorized/Invalid api key"}` |
| `GET /trades?market=<valid>` (unauth) | 401 | JSON | `{"error": "Unauthorized/Invalid api key"}` |

So the adapter must:
- Branch on `Content-Type` (or detect plain-text response) — assuming JSON parses on every error will explode on routing-layer 404s.
- Treat `401` as fatal (auth misconfigured or endpoint requires auth we didn't enable) — never as retryable.
- Treat the JSON error keys as **opaque** — no `error_code`, no machine-readable category. Pattern-match strings if you must categorize, and document each match in the adapter for grep-ability.

### `/trades` is auth-gated

`GET /trades?market=<condition_id>` returns 401 without API credentials. Public trade history is **not** available via this endpoint. If we want recent prints for paper-mode slippage modeling, we need either:

- The WS `last_trade_price` event stream (free, requires keeping a process up), or
- Authenticated API access (L2 keys), or
- An on-chain indexer / The Graph subgraph query.

Decision is deferred to Phase 4; flag for the paper-mode-decision learning.

### Rate limits — not hit at the modest probe level

`probe-rate-limit --requests 50 --concurrency 10` against `GET /markets`: zero 429s, zero errors, no `Retry-After` headers. Anonymous burst tolerance is **at least** 50 requests at concurrency 10; the actual ceiling is unknown.

The plan-1 doc asks for the exact rate limit shape; we don't have it yet without escalating the burst, which is rude on a service we'll later depend on. Defer to the first observed 429 in real adapter use, and instrument the adapter to **always** capture `Retry-After`, response headers, and time-of-burst when one is seen.

### Latency observations

| Call | Latency (ms) |
|---|---|
| First `GET /markets` (cold TLS) | ~3818 |
| `GET /markets/{id}` (warm) | ~150–400 |
| `GET /book?token_id=...` (warm) | 178 |
| `GET /markets?closed=true` (warm) | ~250 |

Cold TLS / cold connection pool dominates the first request by ~10x. The adapter must keep an `httpx.AsyncClient` (or equivalent) alive for the lifetime of the bot — per-request clients are wasteful at this scale.

No 5xx, no timeouts, no TLS issues observed in this session.

## Why it matters

- Error parsing must tolerate two distinct content types and the lack of machine-readable codes.
- Auth-gating of `/trades` is structural — any "fetch recent prints" code path has to choose one of three workarounds, not assume the public REST has it.
- Cold-start latency is significant; the adapter's first tick after process start should not block on a Polymarket call without a warmup.

## Implications for the plan

- **Phase 4 (`plan/04-polymarket-adapter.md`):**
  - Error-typing layer: classify by `(status_code, content_type, body_match)` triple; surface as typed events `RetryableError` / `FatalError` / `AuthError` / `NotFound`. Never let raw `httpx.HTTPStatusError` reach the strategy.
  - Adapter startup performs a warmup call (e.g. `GET /markets/{condition_id}` for each subscribed market) so the first tick's CLOB call is on a warm connection.
  - Recent-trades data path must be chosen explicitly (WS subscription vs auth keys vs subgraph). Document in `.claude/rules/05-exchanges.md`.
- **Phase 6 (`plan/06-risk-and-alerting.md`):** rate-limit headers (`Retry-After`, `X-RateLimit-*` if present) flow into the `exchange_rate_limit_remaining` metric (Phase 6a). Add a `RateLimited` alert at `warn` severity.

## Evidence

- `scout/runs/20260508T034440Z-probe-errors.jsonl` — four error shapes captured.
- `scout/runs/20260508T034439Z-get-trades.jsonl` — `/trades` 401 confirmed.
- `scout/runs/20260508T034517Z-probe-rate-limit.jsonl` — 50 reqs / 10 concurrent / 0 429s.

## Open follow-ups

- Find the actual anonymous rate-limit ceiling, ideally without rude bursts. Possibly observed during Phase 4 paper-mode testing on an active market.
- Confirm whether authenticated REST exposes machine-readable error codes (`error_code` field?) that we're missing from the unauth surface.
- Capture a 5xx if/when one occurs; its shape is currently unknown.
