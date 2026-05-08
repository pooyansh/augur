# Phase 3a — Signals platform

**Goal:** Build the signals layer as a real subsystem, not a per-strategy afterthought. CLAUDE.md promises "50 bots watching BTC make 1 Coingecko call, not 50" — that requires a deduping cache + scheduler + freshness guarantee that doesn't exist yet.

This phase is split out of Phase 3 because (a) it's substantial enough to be done badly if folded in, and (b) Phases 4 and 5 don't depend on it — but Phase 7 does. Land it before Phase 7.

## Deliverables

- `src/signals/base.py` — `Signal` ABC. A signal declares its source, sample cadence, freshness tolerance, and the typed shape it emits. Signals are stateless w.r.t. consumers.
- `src/signals/registry.py` — register a signal class, look it up by name (mirrors strategy registry).
- `src/signals/runner.py` — the **shared scheduler**. Holds one in-memory cache per `(signal, params)` tuple. Bots subscribe; the runner samples upstream once per cadence and fans out.
- `src/signals/storage.py` — every sample written to a `signal_samples` table for **backtest replay**. Append-only.
- `src/signals/staleness.py` — per-subscription staleness check. If the cache hasn't been refreshed within `tolerance`, the bot's `on_tick` is **not** invoked with a stale snapshot — it gets a `SignalStale` event and decides what to do (skip vs. reduce risk vs. close).
- A reference implementation for **at least two signals** to prove the abstraction:
  - `src/signals/btc_15min.py` — 15-min BTC sampler (Coingecko + Binance fallback).
  - `src/signals/election_polling.py` (stub) — different cadence (daily), different shape (multi-candidate). Stub is fine; the point is to confirm the ABC isn't BTC-shaped.
- A **multi-source fallback** primitive: when the primary source fails or rate-limits, the runner tries the next configured source for the same logical signal before serving stale.
- Replay harness: given a `(signal, time_range)`, return a deterministic iterable of historical samples for backtests. Pulls from `signal_samples`.

## Design constraints

- A signal **never** calls an exchange adapter. Signals are read-only views of the world.
- A signal **never** persists arbitrary state — only `signal_samples` rows. State that depends on history is the strategy's job.
- A bot's subscription is declared in `config/bots.yaml`, validated at manager startup. Unknown signals fail fast.
- Rate-limit handling lives in the runner, not in the signal class. A signal class describes what to fetch; the runner controls when.

## Open questions to settle in this phase

- Do we want a websocket/streaming signal type (live book mid-price feeding a sub-second strategy), or is everything pull-based on a cron? Pull-based is simpler and sufficient until proven otherwise.
- Where does feature engineering live — inside the signal, or in the strategy? Default: signals emit raw observations, strategies derive features. Revisit only if two strategies derive the same feature.

## Exit criteria

- [ ] Two bots subscribed to the same signal with the same params produce one upstream call per cadence — verified by an integration test that counts outbound HTTP calls.
- [ ] Backtest harness replays 30 days of `btc_15min` samples through a strategy in <10 seconds.
- [ ] A failing primary source falls back to secondary; staleness alerts fire only after both fail.
- [ ] `SignalStale` event reaches `on_tick` and a strategy can act on it (verified with `NullStrategy`).
