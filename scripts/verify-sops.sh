#!/usr/bin/env bash
# verify-sops.sh — pre-commit hook that confirms secrets/*.enc.yaml files are
# sops-encrypted (i.e., contain a top-level "sops:" metadata key).
#
# Any file failing this check is either:
#   a) a plaintext secret accidentally staged — BLOCK the commit.
#   b) a corrupted encrypted file — also block, worth investigating.
#
# Usage: called by pre-commit with staged *.enc.yaml files as arguments.

set -euo pipefail

FAILED=0

for file in "$@"; do
    # grep exits 1 if pattern not found
    if ! grep -q '^sops:' "${file}" 2>/dev/null; then
        echo "ERROR: ${file} does not appear to be sops-encrypted (missing 'sops:' key)."
        echo "       Run: sops --encrypt --in-place ${file}"
        FAILED=1
    fi
done

if [ "${FAILED}" -ne 0 ]; then
    echo ""
    echo "Commit blocked: one or more secrets files are not properly encrypted."
    exit 1
fi

echo "sops verification passed for all checked files."
