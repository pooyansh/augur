"""Alert-layer redaction wrapper.

Alert messages MUST be redacted before being passed to any sink.  This module
provides the helper used by :class:`~src.alerts.router.AlertRouter` to apply
the same :class:`~src.secrets.redaction.RedactionFilter` that is installed on
the root logger.

Hard rule (invariant 4): pre-redaction text must never reach a sink.
"""

from __future__ import annotations

__all__ = ["make_redactor"]

import logging
from collections.abc import Callable

from src.secrets.redaction import RedactionFilter

logger = logging.getLogger(__name__)


def make_redactor(secret_values: list[str]) -> Callable[[str], str]:
    """Create a redactor callable from a list of plaintext secret strings.

    Args:
        secret_values: Plaintext secret strings to mask.

    Returns:
        A callable that takes a string and returns the redacted version.
    """
    filt = RedactionFilter(secret_values)
    return filt._redact


def identity_redactor(text: str) -> str:
    """No-op redactor used when no secrets are configured (e.g. unit tests).

    Args:
        text: Input text.

    Returns:
        ``text`` unchanged.
    """
    return text
