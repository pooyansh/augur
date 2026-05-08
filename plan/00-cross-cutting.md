# Cross-cutting deliverables

These don't belong to one phase — they accrete across phases. Listed here so they don't fall between the cracks. Each entry says **where** it lands and **when** to start it.

## `.claude/` artifacts

`CLAUDE.md` references nine rule files, four slash commands, and two subagents. None of them exist yet. They get created phase-by-phase as the topics they describe become real:

| Artifact | Created in | Owner content |
|---|---|---|
| `.claude/rules/00-stack.md` | Phase 2 | Pinned versions, "no new heavy deps without updating this file" rule |
| `.claude/rules/01-bot-contract.md` | Phase 3 | `BaseBot` interface, lifecycle, what subclasses may/may not override |
| `.claude/rules/02-state-handoff.md` | Phase 3 | Snapshot schema, rehydrate semantics, strategy-to-strategy handoff via `market_history` |
| `.claude/rules/03-secrets.md` | Phase 2 (skeleton) → Phase 9 (rotation calendar, compromise playbook) | sops/age workflow, recipient sets, rotation cadence |
| `.claude/rules/04-alerting.md` | Phase 6 | Severity routing, dedup keys, redaction guarantee |
| `.claude/rules/05-exchanges.md` | Phase 4 (Polymarket half) → Phase 8 (Kalshi half) | Adapter contract, wire-level details, idempotency mapping |
| `.claude/rules/06-risk-controls.md` | Phase 6 | Cap rationale, kill-switch semantics, audit-log invariants |
| `.claude/rules/07-testing.md` | Phase 3 (unit/property) → Phase 7 (paper minimum) | Test tiers, Hypothesis requirements, paper-window minimum |
| `.claude/commands/new-strategy.md` | Phase 7 (alongside the first strategy) | Scaffold a strategy module + test + config block |
| `.claude/commands/new-exchange.md` | Phase 8 (alongside Kalshi) | Scaffold an adapter w/ paper mode + fixture tests |
| `.claude/commands/inspect-state.md` | Phase 5 | Pretty-print latest snapshot |
| `.claude/commands/deploy-bot.md` | Phase 7 (drafted) → Phase 9 (executed) | Paper→live promotion checklist |
| `.claude/agents/bot-developer.md` | Phase 7 | Strategy subagent: scoped tools, no exchange/risk/secret access |
| `.claude/agents/exchange-adapter-developer.md` | Phase 4 | Adapter subagent: higher review bar, requires fixtures + paper integration test |

A rule file ships when its topic is implemented, not before. An empty rule file is worse than no rule file — it implies "we agreed on this" when we haven't.

## Architecture decision records (ADRs)

Forward-looking complement to `learnings/` (which is empirical). Captures the **decision** and the **alternatives we considered**, so a future operator doesn't redo the analysis. One file per decision in `docs/adr/NNNN-title.md`. Numbered, never renumbered, never deleted (supersession is a new ADR that points at the old one).

Initial backlog — write these as the decisions are made, not in advance:

- `0001-hybrid-bot-lifecycle.md` — long-lived process + JSONB snapshot vs. pure respawn (CLAUDE.md already explains; ADR captures the considered alternatives).
- `0002-sops-age-over-vault.md` — why file-encrypted secrets, not a secret manager.
- `0003-spawn-as-subprocess-v1.md` — why subprocess inside the manager container before container-per-bot.
- `0004-paper-mode-backend.md` — testnet vs. in-process simulator (decided in Phase 1).
- `0005-postgres-over-sqlite-or-kv.md` — why the durability tier we picked.

ADRs are short (one page). They are not design docs.

## Glossary

`docs/glossary.md`. Prediction-market vocabulary differs across platforms — token/share/contract, condition/event/market, resolution/settlement/finalization. Pin our usage once and refer to it. Created in Phase 4 when the first concrete vocabulary collision happens; grown as needed.

## Branch / release process

Documented in `README.md` § Contributing. Created when the first non-author contributor (including a subagent operating semi-autonomously) lands a change.

- Trunk-based. `main` is always deployable.
- Feature branches → PR → squash-merge.
- Release = build image, tag with git sha, push, update prod compose to pin that digest. No `:latest` in prod.
- Rollback = re-pin the previous digest and `docker compose up -d`. Must work without a re-build (Phase 9 verifies).

## Postmortem template

`docs/postmortems/_template.md`, created in Phase 9 before any incident is plausible. After the first incident, copy the template, fill it, link from README. Keep it short: timeline, what happened, why, what we changed. No blame.
