# Implementation Plan — PolymarketBidderBot

This directory holds the phased build plan for the prediction-market bot platform described in `CLAUDE.md`. Read in order; later phases assume earlier ones are complete.

The plan is deliberately staged so that the first phase produces **knowledge**, not production code. We do not yet know the real shape of Polymarket's CLOB, Kalshi's REST/FIX behavior, their rate limits, signing quirks, or settlement semantics — those answers determine the abstractions in `BaseBot` and `ExchangeAdapter`. Building the framework before answering them is how the abstractions end up wrong.

## Phases

| # | File | Output | Status |
|---|---|---|---|
| — | [00-cross-cutting.md](00-cross-cutting.md) | `.claude/` rules/commands/agents, ADRs, glossary, branch & release process — created as topics become real, not up front | ongoing |
| 1 | [01-exploratory-bot.md](01-exploratory-bot.md) | A throwaway single-file Polymarket scout bot + a populated `learnings/` directory | not started |
| 2 | [02-foundations.md](02-foundations.md) | Repo skeleton, Postgres + Alembic, sops/age, docker compose, hardened CI, pre-commit | not started |
| 3 | [03-core-abstractions.md](03-core-abstractions.md) | `BaseBot`, `ExchangeAdapter`, `Signal` ABC, snapshot I/O, registry | not started |
| 3a | [03a-signals.md](03a-signals.md) | Signals platform — shared cache + scheduler, multi-source fallback, replay storage, staleness events | not started |
| 4 | [04-polymarket-adapter.md](04-polymarket-adapter.md) | Production Polymarket adapter w/ market lifecycle, position reconciliation, pessimistic paper-mode slippage | not started |
| 5 | [05-manager-supervisor.md](05-manager-supervisor.md) | Manager: spawn, heartbeat, rehydrate, reload, `bots.yaml` schema, live allow-list, drain/status hooks | not started |
| 6 | [06-risk-and-alerting.md](06-risk-and-alerting.md) | Risk caps in `BaseBot.place`, kill switch, audit log, severity-routed alerts, withdrawal allow-list | not started |
| 6a | [06a-observability.md](06a-observability.md) | Metrics, structured logs with `tick_id`, `/healthz`, SLOs, Grafana dashboards (now optional — see 6b) — must land before Phase 7 | not started |
| 6b | [06b-dashboard.md](06b-dashboard.md) | Operator dashboard: thin JSON API on the manager + client-rendered SPA (compute on the device, not the VPS) — per-bot/market/strategy perf, status, failure timeline | not started |
| 7 | [07-first-strategy.md](07-first-strategy.md) | First real strategy (momentum_v1) end-to-end through paper | not started |
| 8 | [08-kalshi-adapter.md](08-kalshi-adapter.md) | Second adapter — proves the abstraction | not started |
| 9 | [09-promotion-and-ops.md](09-promotion-and-ops.md) | Paper→live checklist, VPS hardening, deploy/rollback, runbooks, postmortem template, DB ops | not started |

Phases `3a`, `6a`, and `6b` are inserted (rather than renumbered) so links into the plan stay stable. Read order matches the table.

## Sidetracks

Parallel tracks that hook into the main plan but don't block it. Each sidetrack has its own README and phase files.

| Track | Lives in | Hook-in points | Status |
|---|---|---|---|
| ML / RL signals | [sidetracks/ml/](sidetracks/ml/README.md) | Phase 3a (Signal interface), Phase 6 (audit log), Phase 6a (metrics), Phase 7 (a strategy variant), Phase 9 (promotion gate adds OPE) | not started |

A sidetrack does **not** weaken any main-plan invariant. The ML track in particular is constrained to emit signals, never to bypass risk, idempotency, or kill-switch checks.

## Hard rules (carry through all phases)

- **No live trading until the [`/deploy-bot`](../.claude/commands/deploy-bot.md) checklist passes** and all three locks from `CLAUDE.md` § Critical invariants are set. Paper mode is the default for every strategy.
- **No new heavy deps** without updating `.claude/rules/00-stack.md`.
- **Plaintext secrets never on disk, never in env vars (unmapped), never in logs.**
- **Hot/cold wallet separation.** Hot wallet holds only working float; on-chain allowance is capped, never `MAX_UINT256`; treasury never touches the VPS. Detail in Phases 4, 6, 9.
- **Dev/prod image parity.** Same image digest in dev, CI, and prod — only `.env` and the dev compose override differ. Detail in Phase 2.
- **Every phase ends with a `learnings/` update** if anything surprising was discovered. See [../learnings/README.md](../learnings/README.md).

## How to use this directory

- Each phase file is self-contained: goal, scope, deliverables, exit criteria, open questions.
- Open questions are tracked per phase. When answered, move the answer into either `CLAUDE.md`, the relevant `.claude/rules/*.md`, or `learnings/` — whichever fits — and strike the question.
- Don't extend a phase's scope; add the new work as a numbered phase. Scope creep on phase 1 is the largest risk to this plan.
