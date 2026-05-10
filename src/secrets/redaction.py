"""Log redaction filter — masks secret values in log records.

Any string value in the loaded secrets that is 8 or more characters long is
compiled into a single alternation regex.  Every log record's message and
stringified arguments are scanned and matching substrings replaced with
``***REDACTED***``.

Short values (< 8 chars) are skipped to avoid false positives on common
tokens like ``"true"``, ``"yes"``, or short env names.

Usage::

    from src.secrets.redaction import RedactionFilter
    import logging

    filt = RedactionFilter(["supersecretapikey123", "anotherkey"])
    logging.getLogger().addFilter(filt)
"""

from __future__ import annotations

__all__ = ["REDACTED", "RedactionFilter"]

import logging
import re
from collections.abc import Iterable

REDACTED = "***REDACTED***"

# Minimum length for a secret value to be included in the redaction pattern.
_MIN_SECRET_LEN = 8


class RedactionFilter(logging.Filter):
    """Logging filter that masks secret values in log messages.

    Args:
        secret_values: Iterable of plaintext secret strings to mask.
            Values shorter than 8 characters are silently ignored.
    """

    def __init__(self, secret_values: Iterable[str]) -> None:
        super().__init__()
        patterns = sorted(
            {re.escape(v) for v in secret_values if len(v) >= _MIN_SECRET_LEN},
            key=len,
            reverse=True,  # longest first to avoid partial matches
        )
        if patterns:
            self._pattern: re.Pattern[str] | None = re.compile("|".join(patterns))
        else:
            self._pattern = None

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact secrets from ``record.msg`` and ``record.args`` in-place.

        Always returns ``True`` (the record is never suppressed, only scrubbed).

        Args:
            record: The log record to sanitise.

        Returns:
            Always ``True``.
        """
        if self._pattern is None:
            return True

        record.msg = self._redact(str(record.msg))

        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact(str(v)) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._redact(str(a)) for a in record.args)

        return True

    def _redact(self, text: str) -> str:
        """Replace all secret occurrences in ``text`` with :data:`REDACTED`.

        Args:
            text: Input string, possibly containing secrets.

        Returns:
            Sanitised string.
        """
        if self._pattern is None:
            return text
        return self._pattern.sub(REDACTED, text)
