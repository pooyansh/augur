---
title: Polymarket settlement on the wire
topic: polymarket
observed_on: 2026-05-09
phase: 1
status: current
---

# Polymarket settlement on the wire

Closes Phase 1 exit criterion **#7**: "What does a settlement look like on the wire,
and what does the strategy need to know about it?" Read-only — no wallet required, so
this was unblocked by the disposable-wallet hold.

## What I observed

`scout/runs/20260508T034440Z-find-resolved.jsonl` — one Gamma call to
`GET /markets?closed=true&order=endDate&ascending=false&limit=5` returned five resolved
markets. The fields that carry resolution state, identical across all five rows:

| Field                  | Source | Example value                                 | Meaning                                                      |
|------------------------|--------|-----------------------------------------------|--------------------------------------------------------------|
| `closed`               | Gamma  | `true`                                        | Market is finalized                                          |
| `active`               | Gamma  | `true`                                        | Stays `true` even after close (does not flip)                |
| `acceptingOrders`      | Gamma  | `false`                                       | CLOB stops taking new orders                                 |
| `umaResolutionStatus`  | Gamma  | `"resolved"`                                  | UMA oracle has finalized the outcome                         |
| `outcomes`             | Gamma  | `"[\"Yes\", \"No\"]"`                         | JSON-string array (per `polymarket-api-shape.md`)            |
| `outcomePrices`        | Gamma  | `"[\"0\", \"1\"]"` or `"[\"1\", \"0\"]"`      | **JSON-string array of payouts.** Index-aligned with `outcomes`. Always `0`/`1` for binary markets in the sample — winning side = `1`, losing side = `0` |
| `closedTime`           | Gamma  | `"2026-03-19 23:20:15+00"`                    | When the market closed (string, space-separated, not ISO)    |
| `umaEndDate`           | Gamma  | `"2026-03-19T23:20:15Z"`                      | Same instant as `closedTime` but in ISO-8601                 |
| `endDate`              | Gamma  | `"2028-01-01T05:00:00Z"`                      | **Scheduled end** — far in the future for early-resolved markets. Not a settlement signal. |
| `resolvedBy`           | Gamma  | `"0x65070BE91477460D8A7AeEb94ef92fe056C2f2A7"`| UMA arbitrator EOA address                                   |
| `resolutionSource`     | Gamma  | `""` or `null`                                | Empty in 5/5 sample. Useful when populated, but not reliable.|
| `umaBond`, `umaReward` | Gamma  | `"500"`, `"5"`                                | UMA economics; informational                                 |
| `clobTokenIds`         | Gamma  | `"[<id_yes>, <id_no>]"`                       | The two ERC-1155 token ids; index-aligned with `outcomes`/`outcomePrices` |

CLOB-side, the matching fields on `GET /markets/{condition_id}` (live market in the
get-market capture, snake_case):

```
active = true
closed = false
accepting_orders = true
accepting_order_timestamp = "2025-05-02T15:47:37Z"
end_date_iso = "2026-07-31T00:00:00Z"
```

CLOB exposes `closed` / `accepting_orders` flags but **not the outcome payouts** —
those come from Gamma (`outcomePrices`) or, authoritatively, from the on-chain
ConditionalTokens framework (`reportPayouts(conditionId, payoutNumerators)`), which
the scout did not query.

## What this means for the typed `SettlementEvent`

Minimum fields the strategy needs to act on a settlement:

```python
@dataclass
class SettlementEvent:
    condition_id: str            # canonical market id (per polymarket-api-shape.md)
    token_id: str                # the ERC-1155 the strategy held
    payout: Decimal              # 0 or 1 (binary); fractional if scalar/categorical (not seen yet)
    settled_at: datetime         # closedTime / umaEndDate (UTC)
    resolver: str                # umaResolutionStatus value, or contract address
    raw: dict                    # the upstream Gamma row, for audit
```

`outcomes` + `outcomePrices` zip into `{outcome_name: payout}`; the adapter computes
`payout` for the held `token_id` by mapping through `clobTokenIds`.

## Why it matters

- The Phase 4 adapter's `SettlementEvent` typed-event surface is now specifiable from
  evidence, not guesswork.
- Closes the last unblocked Phase 1 exit criterion. The remaining open item
  (testnet-vs-simulator confirmation) is genuinely wallet-blocked.
- Confirms a structural quirk we already knew (Gamma JSON-string-encodes arrays;
  `polymarket-api-shape.md`): `outcomes`, `outcomePrices`, `clobTokenIds` all need
  `json.loads` before the adapter can use them.
- Reveals two Gamma-only quirks:
  1. `endDate` is the **scheduled** end, not the actual one. The scheduling field
     `closedTime` / `umaEndDate` is what fires the settlement event.
  2. `active` does **not** flip to `false` on close. Filtering active markets requires
     `closed == false` (and `acceptingOrders == true`), not `active == true`.

## Implications for the plan

- **`plan/04-polymarket-adapter.md`:** carry the `SettlementEvent` shape above as the
  spec for the adapter's settlement typed event. Source: Gamma `closed=true` row.
- **`plan/04-polymarket-adapter.md` § paper-mode simulator:** the simulator polls Gamma
  per-market for `closed=true` transitions to fire `SettlementEvent` locally; live mode
  does the same plus an optional on-chain `reportPayouts` cross-check.
- **`learnings/polymarket-api-shape.md`:** add a one-line note that `active` is sticky
  on close — currently implied but not called out. (Follow-up, not blocking.)
- **CLAUDE.md open question:** does not change the "on-chain CLOB / EIP-712" claim —
  settlement is UMA-arbitrated and ConditionalTokens-finalized, which is consistent.

## Evidence

- `scout/runs/20260508T034440Z-find-resolved.jsonl` — 5 resolved markets, all binary,
  all `umaResolutionStatus="resolved"`, all with `outcomePrices` summing to `1`.
- `scout/runs/20260508T034334Z-get-market.jsonl` — CLOB side of an open market for
  comparison (`closed=false`, `accepting_orders=true`).
- Doc references intentionally not cited; this learning is grounded in observed wire
  responses, consistent with the SDK-derived posture used in
  `polymarket-auth-and-signing.md` and `polymarket-idempotency-and-cancels.md`.

## Open follow-ups

- Scalar / categorical markets: do `outcomePrices` ever take fractional values, and
  does the index-alignment story hold? Sample so far is 5/5 binary.
- On-chain `reportPayouts` cross-check: confirm Gamma's `outcomePrices` always agrees
  with ConditionalTokens. Cheap to add once the wallet probe is unblocked.
- CLOB-side settlement event: is there a CLOB endpoint that exposes the settlement
  payload directly (vs. inferring from `closed=true` + Gamma)? Not seen in this pass.
- Trade tape during settlement: confirm `last_trade_price` WS messages stop firing
  once `acceptingOrders=false`. Relevant for the simulator's resting-fill logic.
