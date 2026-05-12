---
phase_added: 3a
last_reviewed: 2026-05-09
---

# 08 — Signals platform

## What a Signal is

A **Signal** is a stateless declaration of what to fetch, how often, and from
which sources.  It is NOT responsible for scheduling, caching, or persistence.
Those are the runner's job.

```python
class Btc15Min(Signal):
    name = "btc_15min"
    cadence_seconds = 900         # runner fetches every 15 min
    tolerance_seconds = 1800      # stale after 30 min without a fresh fetch
    sources = [BtcCoingeckoSource, BtcBinanceSource]  # order matters: fallback

    def parse(self, source_name, raw) -> dict:
        # canonical shape — strategies see this, never the raw wire format
        ...
```

## Core invariants

### 1. Signals emit raw observations; strategies derive features.
`Signal.parse` converts a raw source response into a canonical shape.  It does
NOT compute rolling averages, z-scores, or any derived feature.  Two strategies
that want the same feature should implement it independently (or share a helper
function — not a signal).

### 2. Multi-source order is part of the signal class; it is NOT runtime config.
`Signal.sources` is a `ClassVar[list[type[SignalSource]]]`.  The runner tries
them left-to-right.  Adding or reordering sources requires a code change, not a
config change.  This keeps the fallback strategy visible and testable.

### 3. Rate-limit handling lives in the runner / source layer.
`SignalSource.fetch` may use a `TokenBucket` (from `src/signals/ratelimit.py`)
to self-throttle.  The `Signal` class never sees rate-limit logic.

### 4. Staleness is data, not an exception.
When all sources fail and the cached sample is older than `tolerance_seconds`,
the runner adds the signal name to `SignalSnapshot.stale`.  `BaseBot.run` passes
the snapshot (with the stale set populated) to `on_tick`.  Strategies read
`snapshot.stale` and decide what to do (skip tick, reduce position, etc.).

**No exception is raised for a stale signal.**

### 5. `signal_samples` is append-only.
The same invariant that applies to `audit_log` applies here.  Every
`SignalStorage.append` call inserts a new row.  No UPDATE, no DELETE.
Corrections are never needed — old samples are historical fact.

### 6. Feature engineering lives in strategies, not signals.
See invariant 1.  This is stated twice because it is violated most often.

## Runner dedup guarantee

`SignalsRuntime` maintains one background fetch loop per unique
`(signal_name, params_hash)`.  `subscribe` is idempotent on that key.  50 bots
subscribing to `btc_15min` with the same (empty) params share exactly one loop
and produce one upstream call per cadence.

## Staleness contract (detailed)

```
is_stale = last_fetch_attempt_failed AND (now - cache.observed_at) > tolerance_s
```

The signal is only marked stale when BOTH conditions hold:
1. The most recent fetch cycle ended with all sources failing.
2. The cached sample is older than `tolerance_seconds`.

If only the last fetch failed but the cache is still fresh (within tolerance),
the runner serves the cached sample and does NOT mark it stale.  This is the
expected behaviour during a transient network hiccup.

## Replay protocol

`SignalReplay` implements the same `SignalsProtocol` as `SignalsRuntime`.
Backtests inject `SignalReplay`; the strategy code is unchanged.

```python
replay = SignalReplay(
    registry=signals,
    storage=SignalStorage(session_factory),
    clock=virtual_clock,
    start=backtest_start,
    end=backtest_end,
    params_hash_map={"btc_15min": Btc15Min({}).params_hash},
)
```

`snapshot_for(config)` returns the latest sample at or before
`virtual_clock.now()`.  The backtest advances the clock between calls.

## Adding a new signal

1. Create `src/signals/<my_signal>.py`.
2. Subclass `Signal`; declare `name`, `cadence_seconds`, `tolerance_seconds`,
   `sources`.
3. Implement `parse(source_name, raw) -> canonical_shape`.
4. Decorate the class with `@signal` (from `src/signals/registry`).
5. `autodiscover` picks it up automatically; no manual registration needed.

## Adding a new source to an existing signal

1. Subclass `SignalSource` in the same module.
2. Implement `async def fetch(params) -> Any`.
3. Append the class to the signal's `sources` ClassVar.
4. Write a unit test with a mock HTTP client.

## Signal validation at startup

`src/manager/supervisor.py::validate_bot_signals(entry)` is called before
spawning each bot subprocess.  If any `entry.signals[*].name` is not registered,
the bot fails to spawn with a `ValueError` listing the unknown names and the
available signals.  Unknown signals fail fast — no silent no-ops.

## See also

- `src/signals/base.py` — Signal, SignalSource, SignalsProtocol, SignalSnapshot
- `src/signals/runner.py` — SignalsRuntime (shared cache + scheduler)
- `src/signals/storage.py` — SignalStorage (append-only Postgres writes + replay reads)
- `src/signals/staleness.py` — pure staleness functions
- `src/signals/ratelimit.py` — TokenBucket (used by rate-limited sources)
- `src/signals/replay.py` — SignalReplay (backtest harness)
- `plan/03a-signals.md` — original design spec and exit criteria
