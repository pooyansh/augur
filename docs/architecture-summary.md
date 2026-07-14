# Architecture Summary — Implementation, Methods, Design

Concise reference. Full narrative walkthrough: `architecture.md`. Diagrams: `architecture-diagram.md` (high-level), `architecture-diagram-detailed.md` (internal flows). Rule-by-rule detail: `.claude/rules/*.md`.

## Core loop

- **`BaseBot.run()`** (`src/bots/base.py`) — the only loop that exists. Nine steps per tick: fetch signals → heartbeat → kill-switch check → (optional) provisional-rule evaluation → `on_tick()` → process cancels → process intents via `place()` → persist snapshot (best-effort) → push metrics → sleep. Strategies implement exactly three methods (`on_tick`, `snapshot`, `rehydrate`) and may call two provided helpers (`place()`, `provisional_ruling()`) — nothing else is overridable.
- **`BaseBot.place()`** — the sole path an order can take: dedup by `client_order_id` → risk-cap check → audit `order_submitted` → `adapter.place()` → audit `order_accepted`/`order_rejected`. A strategy cannot bypass caps, the kill switch, idempotency, or the audit trail — all four are enforced here, not in the strategy.

## Manager / process lifecycle

- **`Supervisor`** (`src/manager/supervisor.py`) spawns each bot as a subprocess (`asyncio.create_subprocess_exec("uv","run","python","-m","src.bots.runner",...)`), tracks `BotProcess` per `bot_id`, and watches heartbeats for staleness (auto-respawn on timeout).
- **`reload(new_roster)`** diffs `config/bots.yaml` against the running set; removed entries get SIGTERM → grace → SIGKILL. Triggered by `SIGHUP` (via the `reload` CLI subcommand writing a path + signaling the manager PID).
- **`stop_bot(bot_id, grace_s=30.0)`** *(new)* — the same SIGTERM→grace→SIGKILL sequence, factored out of `reload()`'s removal loop into a standalone method so it can be called on-demand for one bot without touching the roster file. Returns `False` (not an error) if the bot wasn't running.
- **Heartbeat socket** (`src/manager/heartbeat.py`) is **one-way**: bots write JSONL over a unix socket, the manager only reads. There is no command channel from manager → bot through this socket — process control happens via OS signals against the tracked subprocess handle, not messages.

## Pluggable registries — one pattern, two uses

Both `src/signals/registry.py` and `src/rules/registry.py` follow the identical shape: a decorator (`@signal` / `@winning_rule`) registers a class into a singleton registry keyed by name; `autodiscover()` walks the package so nothing needs manual registration; duplicate names raise `ValueError`; unconfigured bots pay zero cost.

| | Signals | Winning rules *(new)* |
|---|---|---|
| Purpose | External data ingestion, shared across bots | Per-bot provisional outcome judgment |
| Evaluation | Async I/O, on a cadence, cached | Pure, synchronous, per-tick, no I/O |
| Naming | Flat (`btc_15min`) | Dotted, market-scoped: `"polymarket.btc_up_or_down_5m.price_compare"` |
| Storage | `src/signals/*.py` | `src/rules/<venue>/<series_slug>/*.py` (nested, mirrors `ml/`'s `<venue>/<series_slug>/` partitioning) |
| Config | `BotEntry.signals: list[SignalSubscription]` | `BotEntry.winning_rule: WinningRuleRef | None` (optional, absent by default) |

The winning-rule framework is deliberately informational-only: `BaseBot.provisional_ruling()` returns the cached `WON`/`LOST`/`UNDECIDED` value; a strategy must explicitly read it and decide what to do. It never auto-cancels or auto-exits, and it is never allowed to feed P&L or the `position` field in `snapshot()` — enforced by convention + a new, explicitly-separate audit kind (`KIND_PROVISIONAL_RULING`, distinct from `KIND_MARKET_SETTLED`) rather than by a technical guardrail, so this is a rule strategy authors must respect, not one the framework can fully enforce mechanically.

## Risk & safety invariants (unchanged, still enforced)

1. **Three locks for live trading** — `--mode live` AND per-bot `live: true` AND the bot id in the manager's live allow-list. Any one missing ⇒ paper mode.
2. **Deterministic `client_order_id`** — `blake2s(f"{bot_id}:{intent_seq}")`, generated only by `BaseBot`, never by a strategy. Retries of the same logical intent replay the same id (idempotency).
3. **`audit_log` is append-only** — no updates, no deletes, enforced at the DB-role level. Corrections are new rows referencing the original.
4. **Global kill switch vs. per-bot stop vs. provisional ruling are three distinct concepts** (see the table in `architecture-diagram-detailed.md`) — don't conflate them when reasoning about "why did this bot stop trading."

## Dashboard

- **Read side**: `src/manager/dashboard/api.py` — every route is `GET`, backed by the `perf_rollup` materialized view (refreshed every 60s) or `bot_state`/`audit_log` directly, queried through the `dashboard_reader` Postgres role (SELECT-only, enforced at the driver level, tested in `tests/integration/test_dashboard_readonly_role.py`).
- **Control side** *(new)*: `src/manager/dashboard/control_api.py` — exactly one route, `POST /api/control/bots/{id}/stop`. Deliberately a separate file/router from the read side so the "GET-only" invariant stays literally true for that router. It never touches Postgres — it's a same-process call into `Supervisor.stop_bot()` — so it doesn't violate "dashboard never writes to the DB" (that invariant is specifically about the database). Every invocation is audit-logged (`KIND_BOT_STOP_REQUESTED`) before the response returns. Still loopback-only (`127.0.0.1`), same as everything else — no new exposure, VM access stays via SSH tunnel (`ssh -L 8090:127.0.0.1:8090 user@vps`), Phase 9 auth is still deferred.
- **Frontend**: Vite + React 18 + TypeScript SPA (`web/`), built to `web/dist/`, served from the same FastAPI app (no CDN). The new "Stop bot" button on `BotDetail.tsx` is confirm-guarded (`window.confirm()` — no dialog component existed in the codebase to reuse) and calls a separately-named `controlApi.stopBot()` client function, kept structurally distinct from the read-only `api` object.

## Offline `ml/` pipeline

- **Sibling package** to `src/`, own `pyproject.toml`, never imported by `src/` — training/data work is fully decoupled from the live trading path.
- **Tier 1** (`ml/data_collection/events.py`): one row per settlement window, sourced from Gamma's `/events/keyset` (paginated correctly via `after_cursor` — an earlier implementation used the wrong parameter name, silently looked like a "server bug," was corrected after checking Gamma's public `openapi.json`).
- **Tier 2** (`ml/data_collection/trades.py`): one row per (window, token, second), reconstructed from `data-api.polymarket.com/trades`'s raw trade log (not the coarser `prices-history` endpoint, which was verified to not be a true per-second feed) via full pagination + 1-second bucketing with forward-fill. Resumable via a JSONL checkpoint — this collection can span hours and needs to survive interruption.
- **Storage is market-scoped by construction**: `<venue>/<series_slug>/...` partitioning, dotted naming — `series_id`/`venue` are always explicit function arguments, never hardcoded, so the same code serves any future recurring-market series without modification.
- **Not yet built**: `ml/features/`, `ml/datasets/`, any model training. Today's deliverable is a clean, verified, market-agnostic historical dataset — not a trained model.

## Design decisions worth remembering

- **Registries over ad-hoc branching** — both new pluggable concepts this session (winning rules, and implicitly the `ml/` collector's generality requirement) followed the existing signals-registry pattern rather than inventing a new extension mechanism, on the principle that one well-understood pattern beats several bespoke ones.
- **Additive, never signature-breaking, extension points** — the winning-rule feature was deliberately built as a *provided helper method* (`provisional_ruling()`) rather than a new parameter on `on_tick()`, so zero existing strategies needed to change to remain correct.
- **Narrow, explicit exceptions over precedent creep** — the dashboard's one write route is documented as *the only one*, in its own file, with its own justification, specifically so it can't be quietly used as precedent for adding more DB-touching dashboard routes later.
- **Feature branches stay unmerged until explicitly approved** — every implementation this session (ML collectors, winning-rule framework, dashboard kill-bot) was built on its own branch and left for review; nothing was auto-merged to `master`.
