# Phase 9 — Promotion to live + ops

**Goal:** Operate the platform on a single VPS with one bot live, the rest paper, and runbooks for the events that will happen.

## Deliverables

- VPS bring-up doc in `README.md` § Deploy: age key handoff (out-of-band), docker compose up, postgres backup cron, log rotation.
- `/deploy-bot` checklist executed for `momentum_v1` — including all three locks per invariant 1.
- Runbooks in `docs/runbooks/` (or `learnings/runbooks/`):
  - "Kill switch tripped — what now"
  - "Bot crash-loop — diagnose"
  - "Secret rotation"
  - "Hot-wallet compromise — incident response"
  - "Age key recovery"
  - "Polymarket / Kalshi outage"
  - "Manager won't reload"
- Postgres backup verified by a restore-into-throwaway-DB drill.
- Time sync (`chrony` or `systemd-timesyncd`) verified on the VPS — invariant 7 is a correctness issue, not hygiene.
- Image deployed to the VPS is byte-identical (by digest) to what was tested locally — closes the dev/prod parity loop from Phase 2.

## VPS hardening

The VPS is a single point of compromise. Every item below is a step, not a recommendation.

- **SSH:** key-only auth, root login disabled, non-default port optional, `fail2ban` installed and configured.
- **Firewall:** `ufw` (or equivalent) default-deny inbound; allow only SSH and (if needed) the metrics scrape port from a known IP. No public Postgres, no public manager.
- **Unattended security upgrades** enabled for the host OS.
- **Docker daemon** rootless if feasible; otherwise the docker socket is not mounted into any container, and only the deploy user is in the `docker` group.
- **Egress allow-list (best-effort).** The bot containers reach a known set of endpoints (exchange API, signal sources, alert sinks). Document the list. Egress filtering via firewall or a small forward proxy is a stretch goal; if not feasible, the documented list at least makes anomaly detection possible.
- **Intrusion signals:** auditd or equivalent for SSH sessions and sudo invocations; alert on any sudo usage that wasn't initiated by the deploy procedure.
- **Host secrets at rest:** the VPS disk is encrypted, or the age private key lives only on a mounted USB / KMS-style sidecar, not on the root filesystem. (Pick one and document.)

## Database operations

- **WAL archiving** to a separate volume / off-host bucket so point-in-time recovery is possible — daily logical dumps alone are not enough once `audit_log` matters.
- **Retention policy:** `audit_log` and `signal_samples` grow unboundedly. Define a retention window per table (e.g. `audit_log` → 5 years, `signal_samples` → 1 year, `bot_state` → keep latest N per bot + 30 days). Implement as a scheduled `VACUUM` / archival job.
- **Migrations in production** are forward-only; no destructive `DROP COLUMN` until at least one release cycle of dual-write/dual-read. Add this rule to `.claude/rules/03-secrets.md`'s sibling — or a new `.claude/rules/08-database.md` if rule volume warrants.
- **Restore drill** (already an exit criterion) runs quarterly — not just once.

## Deploy & rollback procedure

Documented in `README.md` § Deploy. Concrete steps, copy-pasteable:

1. Build image; tag with `git rev-parse --short HEAD`; push to registry by digest.
2. Update `docker-compose.prod.yml` to pin the new digest.
3. SSH to VPS; `git pull`; `docker compose pull`.
4. `docker compose exec manager python -m manager.drain` — final snapshots written.
5. `docker compose up -d` — manager restarts with new image; bots rehydrate from snapshots.
6. `docker compose exec manager python -m manager.status --json` — verify all bots healthy and on the new digest within 60 seconds.
7. **Rollback:** revert the digest pin, `docker compose pull && up -d`. Must succeed without rebuilding. Verified on every deploy.

## Postmortem template

`docs/postmortems/_template.md` exists before any incident. Structure:

- Summary (one paragraph)
- Timeline (UTC, terse)
- What happened
- Why (root cause; resist single-cause framing)
- What we changed (code / config / runbook / rule)
- What we will not change (consciously decided non-actions)

After every `critical` alert that wasn't a planned drill, fill the template. No blame, no fluff.

## Wallet & key operations

- **Hot/cold separation.** The hot wallet (key in tmpfs, key on the VPS) holds only working float — sized at most to ~1 week of expected position notional per the bot's risk caps. Treasury lives in a cold wallet whose key never touches the VPS. Documented sweep procedure: from cold to hot when float runs low; from hot to cold whenever PnL accumulates above the working-float ceiling.
- **On-chain allowance** for the hot wallet is set once, capped at the working float. Re-approval is a deliberate operator action, not automated.
- **Age key prod backup verified.** Before any prod secret is encrypted to the prod recipient, the prod age private key has an offline backup (paper + sealed, or a hardware token). A recovery drill (restore from backup → decrypt one file → re-seal) is performed and logged.
- **Rotation calendar** in `.claude/rules/03-secrets.md`:
  - Hot-wallet key: every 90 days, or immediately on any suspicion of exposure.
  - Exchange API keys: every 90 days.
  - Alert webhooks: on personnel change.
  - Age recipients: on personnel change; never rotate the prod private key without first adding a new recipient and re-encrypting.
- **Compromise playbook.** Documented sequence: trip kill switch → revoke on-chain allowance (`approve(0)`) → sweep hot wallet to cold → rotate key → re-deploy. Rehearsed once before live.

## Exit criteria

- [ ] One live bot has run for ≥7 days within risk caps.
- [ ] A scheduled secret rotation has been performed end-to-end without downtime on healthy bots.
- [ ] An induced kill-switch trip and recovery has been rehearsed.
- [ ] Hot-wallet compromise playbook has been rehearsed end-to-end (on testnet or with a token-with-no-value), including the `approve(0)` step.
- [ ] Age key recovery from offline backup has been performed at least once into a throwaway environment.
- [ ] Hot-wallet balance is within the configured working-float ceiling; treasury balance is in cold storage.
- [ ] Rollback to the previous image digest succeeds without a rebuild — verified once before the live promotion.
- [ ] Postgres point-in-time recovery (WAL replay) tested into a throwaway DB. Logical dumps alone do not pass this criterion.
- [ ] VPS passes a basic hardening checklist: SSH key-only, firewall default-deny inbound, fail2ban active, unattended upgrades on, audit logging on.
