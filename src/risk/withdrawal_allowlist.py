"""Withdrawal address allow-list.

Operator tools that move funds off-exchange MUST check
:meth:`WithdrawalAllowlist.is_allowed` before executing.  Strategies have
**no** withdrawal code path — this gate is consumed by operator tools only.

The allow-list is loaded once at startup from an sops-encrypted secret file
(decrypted to ``/run/secrets/withdrawal_allowlist.yaml`` at entrypoint time).
Schema::

    addresses:
      - "0xABCDEF..."
      - "0x123456..."

Addresses are matched case-insensitively via ``.lower()``.

Missing file semantics: if the file does not exist or cannot be parsed,
the allowlist is empty and **every withdrawal is refused** (fail-closed).
"""

from __future__ import annotations

__all__ = ["WithdrawalAllowlist"]

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("/run/secrets/withdrawal_allowlist.yaml")


class WithdrawalAllowlist:
    """Gate that decides whether a destination address is permitted.

    Designed to be constructed once at startup and queried throughout the
    process lifetime.  The list is immutable after construction.

    Args:
        addresses: Set of lower-cased hex address strings that are permitted.
    """

    def __init__(self, addresses: frozenset[str]) -> None:
        self._addresses = addresses

    @classmethod
    def load(cls, path: Path | None = None) -> WithdrawalAllowlist:
        """Load the allow-list from a decrypted YAML file.

        Missing file, empty file, or YAML parse errors all return an empty
        allowlist (fail-closed — every address is refused).

        Args:
            path: Absolute path to the decrypted YAML.  Defaults to
                ``/run/secrets/withdrawal_allowlist.yaml``.

        Returns:
            :class:`WithdrawalAllowlist` populated from the file, or an empty
            one on any failure.
        """
        if path is None:
            path = _DEFAULT_PATH

        if not path.exists():
            logger.warning("Withdrawal allowlist not found at %s — all withdrawals refused.", path)
            return cls(frozenset())

        try:
            raw = path.read_text(encoding="utf-8")
            data: Any = yaml.safe_load(raw)
        except Exception as exc:
            logger.error(
                "Failed to parse withdrawal allowlist at %s: %s — all withdrawals refused.",
                path,
                exc,
            )
            return cls(frozenset())

        if not isinstance(data, dict):
            logger.error(
                "Withdrawal allowlist at %s did not parse to a dict — all withdrawals refused.",
                path,
            )
            return cls(frozenset())

        raw_addresses = data.get("addresses", [])
        if not isinstance(raw_addresses, list):
            logger.error(
                "Withdrawal allowlist 'addresses' key is not a list at %s — "
                "all withdrawals refused.",
                path,
            )
            return cls(frozenset())

        normalised = frozenset(addr.lower() for addr in raw_addresses if isinstance(addr, str))
        logger.info("Withdrawal allowlist loaded: %d address(es) permitted.", len(normalised))
        return cls(normalised)

    def is_allowed(self, address: str) -> bool:
        """Return ``True`` if ``address`` is on the allow-list.

        Comparison is case-insensitive.

        Args:
            address: Destination address string to validate.

        Returns:
            ``True`` when the address is explicitly permitted; ``False`` when
            it is absent from the list (including when the list is empty).
        """
        return address.lower() in self._addresses

    def __len__(self) -> int:
        """Return the number of permitted addresses."""
        return len(self._addresses)
