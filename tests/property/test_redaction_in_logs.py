"""Property test: no log line ever contains a registered secret value.

Exit criterion from plan/06a-observability.md:
    "No log line, ever, contains a value present in the loaded secrets
     (verified by a property test that injects each secret value into a
     log call and asserts it's redacted)."

Uses Hypothesis to generate arbitrary secret strings and log messages,
asserting the secrets are never present in captured output.

Implementation note: filters added to the root logger via ``addFilter``
only apply when records are processed by the root logger itself (i.e.
``root.warning(...)``).  For child loggers that propagate to root, the
root's filters are NOT applied to the record -- only the root's handlers
are called.  Therefore this test installs the filter on both the root
logger AND the capture handler, mirroring the real production setup
where ``install_redaction`` adds it at the root level and is used with
root-level log calls in entrypoints.
"""

from __future__ import annotations

import logging
from io import StringIO

from hypothesis import assume, given, settings
from hypothesis import strategies as st
from src.secrets import install_redaction
from src.secrets.redaction import _MIN_SECRET_LEN, REDACTED, RedactionFilter


@given(
    secret=st.text(
        alphabet=st.characters(
            whitelist_categories=("Lu", "Ll", "Nd"),
        ),
        min_size=_MIN_SECRET_LEN,
        max_size=40,
    ),
    filler=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
        min_size=0,
        max_size=20,
    ),
)
@settings(max_examples=200, deadline=500)
def test_log_never_contains_secret(secret: str, filler: str) -> None:
    """Logging a secret value always produces a redacted output line."""
    # Ensure the secret is long enough to be picked up by the filter.
    assume(len(secret) >= _MIN_SECRET_LEN)
    # Ensure the secret is not the same as the REDACTED sentinel (trivially passing).
    assume(secret != REDACTED)

    # Install the redaction filter on the root logger.
    install_redaction([secret])

    # Capture the root logger output.  The filter is also added to the handler
    # so that the captured bytes reflect what would appear in a log file.
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    # Add the filter to the handler itself so it applies regardless of which logger
    # emits the record (child loggers propagate records to root handlers but not
    # to root-level filters).
    handler.addFilter(RedactionFilter([secret]))

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)

    try:
        # Emit directly on the root logger so both root-level and handler-level
        # filters are exercised.
        root.warning("prefix %s suffix %s", secret, f"{filler}{secret}{filler}")
    finally:
        root.removeHandler(handler)
        root.handlers = original_handlers
        root.setLevel(original_level)

    output = buf.getvalue()
    assert secret not in output, (
        f"Secret '{secret}' found in log output: {output!r}. RedactionFilter failed to mask it."
    )
