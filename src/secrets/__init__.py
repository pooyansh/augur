"""Secrets package — loader, redaction filter, and install helper.

Public API:
    :func:`install_redaction` — convenience function that adds a
    :class:`~src.secrets.redaction.RedactionFilter` to the root logger.
    Idempotent: replaces any previously installed instance.
"""

from __future__ import annotations

__all__ = ["install_redaction"]

import logging
from collections.abc import Iterable

from src.secrets.redaction import RedactionFilter


def install_redaction(secret_values: Iterable[str]) -> None:
    """Add a :class:`RedactionFilter` to the root logger.

    Idempotent: removes any previously installed :class:`RedactionFilter`
    before installing the new one (safe to call on secrets rotation).

    Args:
        secret_values: Plaintext strings to mask in all log output.
    """
    root = logging.getLogger()

    # Remove any existing redaction filter.
    root.filters = [f for f in root.filters if not isinstance(f, RedactionFilter)]

    filt = RedactionFilter(secret_values)
    root.addFilter(filt)
