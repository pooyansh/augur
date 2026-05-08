# Phase 1 — Exploratory Bot (Polymarket scout)

**Goal:** Answer concrete questions about Polymarket's API by talking to it from a single throwaway Python script. Produce written findings in `learnings/` that will shape the real `ExchangeAdapter` interface in Phase 3.

**Non-goal:** Do not build any of the framework yet. No `BaseBot`, no Postgres, no manager, no Docker, no sops. One file, one virtualenv, one notebook of findings.

## Why this phase exists

The `ExchangeAdapter` ABC promised in `CLAUDE.md` (`src/exchanges/base.py`) has to express both Polymarket's on-chain CLOB with EIP-712 signed orders **and** Kalshi's REST/FIX. If we sketch that interface from documentation alone we will get the typed-event surface, idempotency story, and paper-mode boundary wrong. This phase exists to prevent that.

Kalshi is intentionally out-of-scope here — pick the harder of the two (Polymarket) first. Phase 8 will repeat this exercise for Kalshi and reconcile.

## Scope

A single file (e.g. `scout/polymarket_scout.py`) with a tiny CLI that exercises, against Polymarket's public CLOB API and — if available — its testnet / Mumbai / Amoy environment:

1. **Discovery (read-only, no auth).**
   - List active markets; inspect the schema returned per market (token IDs, condition IDs, end dates, resolution sources).
   - Pull a single market's orderbook and last trades. Record latency and payload size.
   - Subscribe to whatever streaming surface exists (websocket / SSE) and log a few minutes of book updates. Note message shapes, sequence numbering, gap-recovery behavior.

2. **Auth & signing (testnet first, mainnet only if testnet is unusable).**
   - Generate a throwaway funded wallet on a test environment. Document the funding path.
   - Sign and submit a single resting limit order well off the touch (so it won't fill).
   - Cancel that order. Note the cancel-by-id semantics (signed cancel? batch?).
   - Re-submit the **same** intent with the same `client_order_id`-equivalent (whatever Polymarket calls it, often `salt` / nonce) — confirm whether the API rejects duplicates or silently accepts. This is the foundation for our idempotency contract.

3. **Failure surface.**
   - Submit deliberately bad orders: insufficient allowance, wrong tick size, expired signature, rate-limit burst. Record error shapes (HTTP code, body, fields).
   - Disconnect mid-stream. Record reconnect/resume semantics.

4. **Settlement.**
   - Find a market that recently resolved. Pull the resolution payload. Confirm what the adapter would need to expose as a typed `SettlementEvent`.

5. **Paper-mode reality check.**
   - Decide whether Polymarket's testnet is reliable enough to be our paper-mode backend, or whether paper mode must be a pure in-process simulator fed by live mainnet data. This decision drives Phase 4.

## Deliverables

- `scout/polymarket_scout.py` — single-file CLI. Throwaway code; do not optimize for reuse.
- `scout/requirements.txt` — pinned. Likely `httpx`, `websockets`, `eth-account`, `web3` or `py-clob-client` if it exists and is usable.
- `scout/runs/` — raw response captures (jsonl) from each subcommand, kept small and gitignored if they contain anything sensitive.
- One or more entries in `learnings/` (see template). Minimum:
  - `learnings/polymarket-api-shape.md`
  - `learnings/polymarket-auth-and-signing.md`
  - `learnings/polymarket-idempotency-and-cancels.md`
  - `learnings/polymarket-errors-and-rate-limits.md`
  - `learnings/polymarket-paper-mode-decision.md`

## Hard rules during this phase

- **No mainnet funds beyond a hard cap of $20-equivalent**, and only after testnet has been exhausted. Prefer testnet.
- The wallet private key used here is **disposable** and never reused for production. It does not get committed even encrypted — it lives in a local file outside the repo.
- The scout script is allowed to be ugly. It is **not** allowed to leak its key into logs, tracebacks, or pasted output. Add a redaction wrapper around the logger from day one — it's the one piece of "production hygiene" we keep.
- No premature abstraction. If you find yourself writing a class hierarchy, stop and put the observation in `learnings/` instead.

## Exit criteria

You can answer all of these in writing, with evidence (a captured response, a log line, a code snippet) backing each answer:

- [ ] What is the canonical market identifier we'll thread through the system? (token id? condition id? slug?)
- [ ] What is the minimum set of fields needed to express an `OrderIntent` that Polymarket will accept?
- [ ] How does Polymarket guarantee (or fail to guarantee) idempotency on retried submissions? What field plays the role of `client_order_id`?
- [ ] What is the cancel surface — by id, by market, batch?
- [ ] What error categories must the adapter distinguish (retryable / fatal / risk-tripping)?
- [ ] What are the actual rate limits, and how are they signaled (headers? 429s?)?
- [ ] What does a settlement look like on the wire, and what does the strategy need to know about it?
- [ ] Is testnet our paper-mode backend, or do we build an in-process simulator?

## Open questions to flag back to CLAUDE.md / rules

If any of these turn out to contradict CLAUDE.md, the rule book changes — not the findings.

- The CLAUDE.md description of Polymarket as "on-chain CLOB with EIP-712 signed orders" — confirm this is still accurate (vs. a hosted matching engine that batches on-chain).
- Whether `BaseBot` generating `client_order_id` is even meaningful on Polymarket, or whether the equivalent is a signature-bound nonce that can't be pre-generated cleanly.
- Whether the 15-minute tick cadence in CLAUDE.md is compatible with Polymarket's tick size and book depth on the markets we'd actually trade.

## Time box

Two focused sessions. If you're past that and still on phase 1, it means questions are getting deeper, not closer to closed — bring the open list back here and decide which ones are actually phase-3 problems in disguise.
