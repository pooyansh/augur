---
phase_added: 2
last_reviewed: 2026-05-09
---

# 03 — Secrets management (sops + age)

## Layout

```
secrets/
├── .sops.yaml            # age recipients (host keys, dev keys, CI key)
├── exchanges.enc.yaml    # API keys, wallet private keys, per exchange/env
├── alerts.enc.yaml       # Slack, Discord, Telegram webhooks
└── infra.enc.yaml        # DB passwords, internal tokens
```

All files are sops-encrypted and **committed to the repo**.  Plaintext never
appears on disk outside the tmpfs mount.

## Recipient sets

`.sops.yaml` lists three recipient groups:

| Group | Who | Key location |
|---|---|---|
| dev | Individual dev age keys | `~/.config/sops/age/keys.txt` |
| ci | CI-only age key | CI secret store (not in the image) |
| prod | VPS host age key | `~/.config/sops/age/keys.txt` on the VPS host (mounted, never copied into image) |

## Workflow

```bash
# One-time: generate dev age key and register its public key in .sops.yaml
make secrets-init

# Edit a secrets file (decrypts → $EDITOR → re-encrypts on save)
make secrets-edit FILE=exchanges

# Rotate recipients (e.g. after adding a new dev key)
make secrets-rotate FILE=exchanges
```

## Entrypoint decryption

`docker/entrypoint.sh` decrypts `secrets/*.enc.yaml` into `/run/secrets/*.yaml`
on a **tmpfs mount** before exec'ing the app.  The age private key is mounted
from the host's `~/.config/sops/age/keys.txt` — never copied into the image.

## Redaction filter (mandatory)

Every entrypoint MUST call `configure_logging_with_redaction(loaded_secrets)`
(from `src.secrets.install`) before any other code runs.  This installs the
`RedactionFilter` on the root logger, masking any loaded secret value that
appears in log output.

Short values (< 8 chars) are excluded from the filter to avoid false positives.

## Hard rules

- Plaintext secrets must NEVER be committed, logged, or sent to alert channels.
- Secrets must NEVER be set as environment variables except via explicit mapping
  in the entrypoint (with `export VAR=$(cat /run/secrets/...)` as the sole
  mechanism).
- `docker inspect` output must never reveal secret values.
- The pre-commit hook runs `sops --verify` on `secrets/*.enc.yaml` and blocks
  any unencrypted YAML in `secrets/`.

## TODO (Phase 9)

- Rotation calendar (quarterly key rotation schedule).
- Compromise playbook (steps when a private key is suspected leaked).
- CI recipient key rotation procedure.
