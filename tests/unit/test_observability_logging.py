"""Unit tests for src/observability/logging.py.

Tests:
- configure_logging is idempotent.
- bind_tick_id injects tick_id into the contextvar.
- RedactionFilter masks secret values in log output.
"""

from __future__ import annotations

import logging


def _reset_logging_configured() -> None:
    """Reset the module-level _configured flag so configure_logging runs again."""
    import src.observability.logging as obs_logging

    obs_logging._configured = False


def test_bind_tick_id_sets_contextvar() -> None:
    """tick_id_var is set inside bind_tick_id and cleared after."""
    from src.observability.context import tick_id_var
    from src.observability.logging import bind_tick_id

    assert tick_id_var.get(None) is None

    with bind_tick_id("abcd1234"):
        assert tick_id_var.get(None) == "abcd1234"

    assert tick_id_var.get(None) is None


def test_configure_logging_idempotent() -> None:
    """Calling configure_logging twice does not raise or duplicate handlers."""
    import src.observability.logging as obs_logging

    _reset_logging_configured()
    obs_logging.configure_logging(level="info")
    handler_count_after_first = len(logging.getLogger().handlers)

    # Second call should be a no-op.
    obs_logging.configure_logging(level="debug")
    handler_count_after_second = len(logging.getLogger().handlers)

    assert handler_count_after_first == handler_count_after_second

    _reset_logging_configured()


def test_redaction_filter_masks_secret_in_root_logger(capsys: object) -> None:
    """A log call containing a secret is redacted when the filter is installed."""
    from src.secrets import install_redaction
    from src.secrets.redaction import RedactionFilter

    secret = "supersecretapikey123"
    install_redaction([secret])

    # Emit a log line via stdlib root logger.
    root = logging.getLogger()
    root.warning("This contains the secret: %s", secret)

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert secret not in captured.out
    assert secret not in captured.err

    # Clean up — remove the redaction filter.
    root.filters = [f for f in root.filters if not isinstance(f, RedactionFilter)]


def test_bind_tick_id_propagates_to_contextvar() -> None:
    """Inside bind_tick_id the contextvar returns the bound id."""
    from src.observability.context import current_tick_id
    from src.observability.logging import bind_tick_id

    with bind_tick_id("deadbeef"):
        assert current_tick_id() == "deadbeef"
    assert current_tick_id() is None
