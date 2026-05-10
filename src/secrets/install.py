"""Entrypoint helper — configure root logging with secret redaction.

Call site contract: manager and bot entrypoints MUST call
:func:`configure_logging_with_redaction` before any other code runs (and
specifically before the first log statement that might emit a secret).

Example (in ``docker/entrypoint.sh`` or the manager ``__main__.py``)::

    from src.secrets.loader import load_secrets
    from src.secrets.install import configure_logging_with_redaction

    secrets = load_secrets()
    configure_logging_with_redaction(secrets)
    # Now safe to import and run the rest of the application.
"""

from __future__ import annotations

__all__ = ["configure_logging_with_redaction"]

import logging
from collections.abc import Mapping
from typing import Any

from src.secrets import install_redaction


def _flatten_leaf_strings(data: Mapping[str, Any]) -> list[str]:
    """Recursively extract all leaf string values from a nested mapping.

    Args:
        data: Potentially nested dict of secret values.

    Returns:
        Flat list of all string leaf values.
    """
    result: list[str] = []
    for value in data.values():
        if isinstance(value, str):
            result.append(value)
        elif isinstance(value, dict):
            result.extend(_flatten_leaf_strings(value))
    return result


def configure_logging_with_redaction(loaded_secrets: Mapping[str, Any]) -> None:
    """Flatten all leaf string values from ``loaded_secrets`` and install the
    redaction filter on the root logger.

    Call this once, as early as possible in the process lifecycle — before
    any code that might log a secret value.  The function is idempotent; a
    second call replaces the filter collection with the new values.

    Args:
        loaded_secrets: The mapping returned by
            :func:`~src.secrets.loader.load_secrets` (or any compatible dict).
    """
    all_values: list[str] = []
    for section in loaded_secrets.values():
        if isinstance(section, dict):
            all_values.extend(_flatten_leaf_strings(section))
        elif isinstance(section, str):
            all_values.append(section)

    install_redaction(all_values)
    logging.getLogger(__name__).debug(
        "Redaction filter installed with %d secret values.", len(all_values)
    )
