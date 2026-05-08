# Phase 5 — Manager / supervisor

**Goal:** The long-lived process that reads `config/bots.yaml`, spawns bots, supervises heartbeats, and orchestrates rehydrate on failure.

## Deliverables

- `src/manager/supervisor.py` — spawn-as-subprocess (v1 per `CLAUDE.md`); identical interface for future container-per-bot (v2).
- Heartbeat loop over a unix socket. Two missed beats or non-zero exit → respawn from latest snapshot.
- `python -m manager.reload` — diff `config/bots.yaml` against running set; start new, stop removed, leave healthy unchanged.
- Drain-on-SIGTERM: bots get a chance to write a final snapshot before respawn during rolling deploys.
- `/inspect-state <bot_id>` slash command implementation in `.claude/commands/inspect-state.md`.

## `config/bots.yaml` schema (formalized here)

Pinned in this phase because the manager owns parsing and validation. Pydantic model in `src/manager/config.py`. Schema:

- `bots[].id` — stable, unique, used as the partition key for `bot_state` and the label key in metrics.
- `bots[].strategy` — registered strategy name; must resolve via the registry.
- `bots[].market` — `{exchange, market_id}`; market id is the canonical identifier from Phase 1.
- `bots[].mode` — `paper` (default) or `live`. Setting `live` here is one of the three locks (invariant 1).
- `bots[].schedule` — cron-like string; validated against the strategy's declared `Schedule`.
- `bots[].signals[]` — list of `{name, params}` subscriptions; validated against signal registry (Phase 3a).
- `bots[].risk` — caps overriding strategy defaults: `max_position_notional`, `max_daily_loss`, `max_orders_per_minute`.
- `bots[].secrets` — references **by name** to keys in `secrets/exchanges.enc.yaml` etc. Plaintext values here would fail the secret scan.
- `bots[].alerts` — optional per-bot routing override.

Manager startup performs full schema validation **before** spawning any bot; a single invalid entry fails the whole load (no partial roster).

## Manager-level live allow-list mechanism

The "manager-level live allow-list" lock is concrete:

- A separate, sops-encrypted file: `secrets/live_allowlist.enc.yaml` containing a list of `bot_id`s permitted to run in `live` mode.
- Loaded once at manager startup; not hot-reloadable. Adding a bot to live trading is a deliberate manager restart, not a config reload.
- A bot whose YAML says `mode: live` but whose `id` is not in the allow-list is downgraded to `paper` at spawn and a `critical` alert is emitted. Failing closed is the point.
- Editing this file requires the prod age key (the same one used for production secrets) — so promotion to live cannot happen without prod-key access.

## Deploy & rollback hooks (manager-side)

The manager exposes the primitives that Phase 9's deploy procedure depends on:

- `python -m manager.drain` — stop accepting new ticks, let in-flight ticks complete, write final snapshots, exit cleanly. Used before `docker compose up` with a new image.
- `python -m manager.status --json` — current bot roster, mode (paper/live), heartbeat ages, snapshot lag, last error per bot. Used by deploy scripts and the `/healthz` check.
- Image digest is exposed in `/metrics` as a `build_info` gauge so a Grafana panel makes "what's running right now" obvious. Mismatched digests across services indicate a half-applied deploy.

## Exit criteria

- [ ] Kill a running bot's process; manager rehydrates a replacement that picks up within one tick.
- [ ] `manager.reload` adds and removes bots without disrupting unrelated ones.
- [ ] Rolling restart preserves a bot's in-memory state (verified by snapshot continuity in `bot_state`).
- [ ] A bot configured `mode: live` but absent from the live allow-list spawns in `paper` mode and emits `critical`.
- [ ] An invalid `config/bots.yaml` (unknown signal, bad schedule, missing secret reference) fails manager startup with a clear error and zero bots spawned.
- [ ] `manager.drain` returns only after every bot has written a final snapshot.
