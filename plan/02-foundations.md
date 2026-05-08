# Phase 2 — Foundations

**Goal:** Stand up the repo skeleton so all later phases drop into place. No business logic.

## Deliverables

- `pyproject.toml` with the exact stack from `CLAUDE.md` § Stack and `.claude/rules/00-stack.md`. Python 3.12, `mypy --strict`, ruff, pytest, hypothesis, polars, sqlalchemy 2.x, asyncpg, alembic.
- `src/` layout matching `CLAUDE.md` § Project layout. Empty packages with `__init__.py` are fine.
- `docker-compose.yml` + `docker-compose.dev.yml`. Services: `manager`, `postgres`, `pgadmin` (dev only).
- `docker/Dockerfile.manager`, `docker/Dockerfile.bot`, `docker/entrypoint.sh` (sops decrypt → exec → tmpfs mount for secrets).
- `secrets/.sops.yaml` with at least a dev age recipient. `make secrets-init`, `make secrets-edit FILE=...` Makefile targets.
- Alembic initialized; first migration creates `bot_state`, `audit_log`, `kill_switch` tables (schemas defined in Phase 3 — stub for now).
- `.pre-commit-config.yaml`: ruff, mypy, sops verify, secret scan (gitleaks or similar).
- `.env.example`, `README.md` with the exact "Running it" block from `CLAUDE.md`.
- CI: lint + mypy + unit tests on push. **Dependency CVE scan** (`pip-audit` or equivalent) on a schedule. **Container image scan** (Trivy or equivalent) on every image build. **Secret scan** server-side over full git history (gitleaks `--log-opts=--all`), not just the staged diff. CI cannot reach live exchanges — outbound network in tests is restricted to test fixtures and localhost. No deploy step.
- Lockfile: `uv.lock` (or equivalent) committed. Reproducible builds — same lockfile in dev, CI, and prod images.

## Container hardening (applies to both manager and bot images)

- `ulimit -c 0` in `entrypoint.sh` — **no core dumps.** Decrypted keys live in process memory; a core file on crash would persist them to disk.
- `--security-opt=no-new-privileges` in compose.
- `cap_drop: [ALL]` and add back only what's needed (none, for these workloads).
- `read_only: true` filesystem; `/tmp` and the secrets tmpfs are the only writable mounts.
- Run as a non-root UID baked into the image.
- No `:latest` tags — images are pinned by digest in the prod compose file.

## Age key handling

- `make secrets-init` generates a **dev** age key locally (`~/.config/sops/age/keys.txt` on the dev machine, mode 0600). Never committed.
- A separate **prod** age recipient is registered in `.sops.yaml` from day one, even though no secret is decryptable with it yet — adding the recipient later requires re-encrypting every file.
- A **CI** age recipient is registered the same way; its private key lives in the CI secret store, not the repo.
- **Age private-key backup is a documented step**, not implicit. The dev key gets a printed/offline copy; the prod key (Phase 9) gets a paper or hardware-token backup before any secret is encrypted to it. Loss of the prod key = unrecoverable secrets.

## Dev/prod parity

- Same `Dockerfile.manager` / `Dockerfile.bot` build the image used in dev, CI, and prod. The dev override (`docker-compose.dev.yml`) only adds dev-only services and bind mounts; it never substitutes the image.

## Exit criteria

- [ ] `docker compose up` brings the stack up cleanly on a fresh machine.
- [ ] `make secrets-edit FILE=exchanges` round-trips an encrypted file.
- [ ] `pre-commit run --all-files` passes.
- [ ] `mypy --strict src` returns zero errors against the empty packages.
- [ ] Container runs as non-root with `cap_drop: ALL`, read-only rootfs, and `ulimit -c 0` verified inside the running container.
- [ ] `.sops.yaml` contains dev, CI, and prod recipients. The prod private key has a documented offline backup procedure (even if the key itself isn't yet generated).
- [ ] The image tag deployed in dev is byte-identical (by digest) to what Phase 9 will deploy to the VPS.

## Out of scope

- Any bot code. Any adapter code. Any signal code. Anything that depends on Phase 1's findings.
