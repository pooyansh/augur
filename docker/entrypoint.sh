#!/usr/bin/env bash
# entrypoint.sh — decrypt sops secrets to tmpfs, then exec the application.
#
# Security contract:
#   - set -euo pipefail: any unhandled error aborts immediately.
#   - ulimit -c 0: no core dumps. Decrypted key material lives only in process
#     memory; a core file on crash would persist it to disk.
#   - Secrets are decrypted to /run/secrets (tmpfs, size=4m, mode=0700).
#     The tmpfs is configured in docker-compose.yml; this script only writes
#     into it.
#   - The age private key is mounted read-only from the host at
#     ${SOPS_AGE_KEY_FILE:-/run/age/keys.txt}. It is NEVER copied into the
#     image or persisted to disk inside the container.
#   - exec "$@" replaces this shell with the application process so that PID 1
#     is the app and signals (SIGTERM, etc.) propagate correctly.

set -euo pipefail
ulimit -c 0

SECRETS_SRC="${SECRETS_DIR:-/app/secrets}"
SECRETS_DEST="/run/secrets"
SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-/run/age/keys.txt}"

export SOPS_AGE_KEY_FILE

# Decrypt every *.enc.yaml in the secrets directory to the tmpfs mount.
# The output file name strips the ".enc" infix: exchanges.enc.yaml -> exchanges.yaml
if [ -d "${SECRETS_SRC}" ]; then
    for enc_file in "${SECRETS_SRC}"/*.enc.yaml; do
        [ -f "${enc_file}" ] || continue  # glob expands to literal if no match

        base="$(basename "${enc_file}" .enc.yaml)"
        dest="${SECRETS_DEST}/${base}.yaml"

        sops --decrypt \
             --input-type yaml \
             --output-type yaml \
             "${enc_file}" > "${dest}"

        chmod 0600 "${dest}"
        echo "[entrypoint] decrypted ${enc_file} -> ${dest}"
    done
else
    echo "[entrypoint] WARNING: secrets directory ${SECRETS_SRC} not found; skipping decryption"
fi

exec "$@"
