# secrets/

This directory holds sops-encrypted YAML files. Encrypted `*.enc.yaml` files
ARE committed to the repository. Decrypted `*.yaml` files are git-ignored.

## Seeding encrypted files for the first time

1. Run `make secrets-init` to generate your dev age key (if you haven't already).
   It prints the public key — paste it into `.sops.yaml` replacing the dev placeholder.

2. Create each secrets file via:

   ```bash
   make secrets-edit FILE=exchanges
   make secrets-edit FILE=alerts
   make secrets-edit FILE=infra
   ```

   This opens your `$EDITOR` with an empty (or decrypted) YAML file. Save and
   close — sops re-encrypts automatically.

## File inventory

| File | Contents |
|------|----------|
| `exchanges.enc.yaml` | API keys, wallet private keys (per exchange/environment) |
| `alerts.enc.yaml` | Slack, Discord, Telegram webhook URLs |
| `infra.enc.yaml` | Database passwords, internal tokens |

## Key rotation

To add a recipient or rotate keys:

```bash
# Add the new recipient public key to .sops.yaml, then:
make secrets-rotate FILE=exchanges
make secrets-rotate FILE=alerts
make secrets-rotate FILE=infra
```

## Offline backup

The prod age private key (Phase 9) MUST have a documented offline backup
(printed paper copy or hardware token) before any secret is encrypted to it.
Loss of the prod key = unrecoverable secrets.
