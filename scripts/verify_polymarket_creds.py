"""Verify Polymarket credentials load correctly and can authenticate.

Decrypts secrets/exchanges.enc.yaml via sops and checks:
1. Config parses without errors.
2. A public CLOB endpoint is reachable.
3. An authenticated endpoint returns 200 (L2 headers work).

Usage:
    SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt uv run python scripts/verify_polymarket_creds.py
"""

import asyncio
import subprocess
import sys

import httpx
import yaml


def decrypt_secrets() -> dict:
    result = subprocess.run(
        ["sops", "--decrypt", "secrets/exchanges.enc.yaml"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR decrypting secrets: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return yaml.safe_load(result.stdout)


async def main() -> None:
    print("Loading secrets...")
    raw = decrypt_secrets()

    from src.exchanges.polymarket import load_polymarket_config

    config = load_polymarket_config(raw["polymarket"])
    print(f"  wallet_address : {config.wallet_address}")
    print(f"  l2_api_key     : {config.l2_api_key[:8]}...")
    print(f"  signature_type : {config.signature_type}")

    async with httpx.AsyncClient(timeout=10) as client:
        # Public endpoint — no auth needed
        print("\nChecking public CLOB endpoint...")
        resp = await client.get(f"{config.clob_host}/markets?limit=1")
        print(f"  GET /markets -> {resp.status_code}")
        if resp.status_code != 200:
            print(f"  ERROR: {resp.text[:200]}", file=sys.stderr)
            sys.exit(1)

        # Authenticated endpoint — L2 headers
        print("Checking authenticated endpoint (GET /data/orders)...")
        import base64
        import hashlib
        import hmac
        import time

        ts = str(int(time.time()))
        msg = ts + "GET" + "/data/orders" + ""
        sig = hmac.new(
            base64.urlsafe_b64decode(config.l2_secret),
            msg.encode(),
            hashlib.sha256,
        ).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).decode()
        headers = {
            "POLY_ADDRESS": config.wallet_address,
            "POLY_SIGNATURE": sig_b64,
            "POLY_TIMESTAMP": ts,
            "POLY_API_KEY": config.l2_api_key,
            "POLY_PASSPHRASE": config.l2_passphrase,
        }
        resp2 = await client.get(f"{config.clob_host}/data/orders", headers=headers)
        print(f"  GET /data/orders -> {resp2.status_code}")
        if resp2.status_code == 200:
            print("\nSUCCESS — credentials are valid and working.")
        else:
            print(f"  Response: {resp2.text[:300]}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
