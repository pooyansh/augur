---
phase_added: 3
last_reviewed: 2026-05-09
---

# 02 — State snapshot schema and strategy handoff

## `bot_state` table (Phase 3 schema)

| Column | Type | Notes |
|---|---|---|
| `bot_id` | `VARCHAR(255) PK` | Stable bot identifier; also the client_order_id seed |
| `snapshot_at` | `TIMESTAMPTZ NOT NULL` | UTC write time; server default `now()` |
| `version` | `INTEGER NOT NULL` | Monotonically increasing per bot |
| `market_id` | `TEXT NOT NULL` | Indexed for the deferred market_history view |
| `state` | `JSONB NOT NULL` | Full strategy state; see schema below |

Indexes:
- `(market_id, snapshot_at DESC)` — supports the deferred `market_history` view.
  The SQL view itself ships in Phase 5; the index is created now so view
  creation is instant and requires no table scan.
- `(bot_id, snapshot_at DESC)` — fast latest-snapshot lookup for rehydration.

## JSONB `state` required keys

Every strategy's `snapshot()` dict MUST include:

```json
{
  "intent_seq": 42,
  "position": "150.00",
  "last_decision_at": "2026-05-09T12:00:00+00:00",
  "_version": 7
}
```

| Key | Type | Set by |
|---|---|---|
| `intent_seq` | int | BaseBot._persist_snapshot |
| `position` | str (Decimal) | BaseBot._persist_snapshot |
| `last_decision_at` | str (ISO-8601) | BaseBot._persist_snapshot |
| `_version` | int | BaseBot._persist_snapshot (incremented each write) |

Strategy-specific keys are added alongside these.

## Rehydrate semantics

1. Manager reads `SELECT * FROM bot_state WHERE bot_id = $1`.
   (One row per bot — upserted on each tick.)
2. Manager passes `row.state` to `BaseBot.rehydrate(snapshot)`.
3. `rehydrate` restores `_intent_seq`, `_position_notional`, and any
   strategy-specific fields.
4. Idempotent: calling `rehydrate` twice with the same snapshot leaves the
   bot in the same state as calling it once.

## Strategy-to-strategy handoff (via market_history view — DEFERRED to Phase 5)

When Strategy X is replaced by Strategy Y on the same market:

1. X's final `_persist_snapshot()` call writes the terminal snapshot.
2. The manager passes that snapshot (or the most recent prior one) to Y's
   `rehydrate()`.
3. Y reads the keys it cares about; unknown keys from X are ignored.

The `market_history` SQL view joins `bot_state` with historical strategy names
to let Y query X's trading history for context (e.g. recent fill prices).

**Status: deferred to Phase 5.**  The `(market_id, snapshot_at DESC)` index is
in place; the `CREATE VIEW` statement ships in Phase 5.

## Snapshot failure policy (invariant 6)

- Snapshot failures emit a `warn` log (and a Slack/Discord alert once Phase 6
  wires the sinks).
- The tick continues normally after a failure.
- The manager's worst case: on next crash-recovery, it rehydrates from the
  last *successful* snapshot.  At most one tick of replay.
