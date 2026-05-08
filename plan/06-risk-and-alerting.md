# Phase 6 — Risk controls + alerting

**Goal:** Make the platform safe to point at real money — even though no live strategy yet exists.

## Deliverables — risk

- `src/risk/caps.py` — per-bot max position notional, max daily loss, max orders per minute. Loaded from `config/bots.yaml`.
- `src/risk/kill_switch.py` — Postgres-backed flag, checked before every order. Tripping cancels open orders across **all** bots and freezes new placement.
- `src/risk/audit.py` — append-only writes to `audit_log` for every `OrderIntent` and `OrderResult`. No deletes, no updates (invariant 5). Corrections are new rows referencing the original.
- All three are enforced inside `BaseBot.place` (invariant 3); strategies cannot bypass.

## Deliverables — wallet & withdrawal controls

- **Withdrawal allow-list.** Any operator tool that moves funds off-exchange reads its destination addresses from an encrypted secret allow-list. The list is loaded at startup; addresses not on it are rejected. Strategies have **no** code path that touches withdrawals.
- **Per-bot secret scoping (forward-looking).** Today the manager loads the union of secrets and passes only the relevant slice to each bot subprocess via the `BotDeps` constructor. When the platform graduates to container-per-bot (the v2 path), each container mounts only its own slice. The interface is designed so that flip is a deployment change, not a code change.
- **Hot-wallet allowance + balance checks** from Phase 4 are wired into the kill switch: a tripped allowance/balance check freezes new placement automatically.

## Deliverables — alerting

- `src/alerts/router.py` with sinks: Slack, Discord, Telegram. Severity routing per `CLAUDE.md` § Alerting.
- Dedup keys on every alert so flapping doesn't spam.
- Webhooks loaded via sops; alert bodies pass through the same redaction filter as logs (invariant 4).

## Exit criteria

- [ ] Cap-tripping property tests (Hypothesis) pass.
- [ ] Killing the switch cancels open paper orders across ≥2 running paper bots in an integration test.
- [ ] An alert with a value matching a loaded secret is redacted before transmission — verified by a test that asserts the outbound body never contains the secret.
