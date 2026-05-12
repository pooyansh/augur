"""JSON redaction helper for the dashboard API.

Walks a JSON-serialisable dict (or list) and applies the Phase 3
:class:`~src.secrets.redaction.RedactionFilter` to every string leaf value.
Used as a helper in endpoint handlers that expose free-form text fields
(``last_error``, ``payload`` echoes, stack-trace tails).
"""

from __future__ import annotations

__all__ = ["JsonRedactor"]

from typing import Any

from src.secrets.redaction import RedactionFilter


class JsonRedactor:
    """Walks a JSON-ish structure and redacts secret strings from every leaf.

    Args:
        secret_values: Iterable of plaintext secret strings to mask.
            Passed directly to :class:`~src.secrets.redaction.RedactionFilter`.
    """

    def __init__(self, secret_values: list[str]) -> None:
        self._filter = RedactionFilter(secret_values)

    def redact(self, obj: Any) -> Any:
        """Recursively redact all string leaves in ``obj``.

        Args:
            obj: A JSON-serialisable value (dict, list, str, int, float, None, bool).

        Returns:
            A new structure with every string leaf passed through the redaction
            filter.  Non-string leaves (int, float, bool, None) are returned
            unchanged.
        """
        if isinstance(obj, str):
            return self._filter._redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact(item) for item in obj]
        return obj

    def redact_string(self, text: str | None) -> str | None:
        """Redact a single optional string field (e.g. ``last_error``).

        Args:
            text: Input string or None.

        Returns:
            Redacted string or None if input was None.
        """
        if text is None:
            return None
        return self._filter._redact(text)
