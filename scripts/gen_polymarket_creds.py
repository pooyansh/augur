"""Generate Polymarket L2 API credentials from a wallet private key.

Run this LOCALLY in your own terminal — never paste your private key into
Claude Code or any shared environment.

Usage:
    uv run python scripts/gen_polymarket_creds.py

The script prompts for your private key (hidden input), calls the Polymarket
CLOB API to derive L2 credentials, and prints the YAML block to paste into
secrets/exchanges.enc.yaml via `make secrets-edit FILE=exchanges`.

The private key is never written to disk or printed.
"""

import asyncio
import getpass
import sys

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds


CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


def derive_creds(private_key: str) -> ApiCreds:
    client = ClobClient(host=CLOB_HOST, key=private_key, chain_id=CHAIN_ID)
    return client.create_or_derive_api_creds()


async def main() -> None:
    print("Polymarket L2 credential generator")
    print("Your private key is never stored or printed.\n")

    private_key = getpass.getpass("Wallet private key (0x...): ").strip()
    if not private_key.startswith("0x") or len(private_key) != 66:
        print("ERROR: expected a 0x-prefixed 64-char hex private key", file=sys.stderr)
        sys.exit(1)

    print("\nDeriving credentials from Polymarket API...")
    try:
        creds = derive_creds(private_key)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    from eth_account import Account
    address = Account.from_key(private_key).address

    print("\n" + "=" * 60)
    print("SUCCESS — paste this into secrets/exchanges.enc.yaml")
    print("Run: make secrets-edit FILE=exchanges")
    print("=" * 60)
    print(f"""
polymarket:
  live:
    wallet_address: "{address}"
    l2_api_key: "{creds.api_key}"
    l2_api_secret: "{creds.api_secret}"
    l2_passphrase: "{creds.api_passphrase}"
    # l1_private_key: "0x..."  # add manually — needed for order signing
""")
    print("=" * 60)
    print("NOTE: l1_private_key is NOT printed — add it manually to the")
    print("secrets file. It is required for placing orders (EIP-712 signing).")


if __name__ == "__main__":
    asyncio.run(main())
