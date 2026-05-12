"""Structured logging setup using structlog with JSON rendering and redaction.

Call :func:`configure_logging` once at process startup (manager __main__ and
bot runner).  Subsequent calls are no-ops (idempotent).

The structlog chain:
  1. merge_contextvars — pulls bot_id/tick_id from structlog's bound context
  2. add_log_level — adds ``level`` key
  3. TimeStamper(utc=True, fmt="iso") — adds ``ts`` key
  4. UnicodeDecoder — handles bytes in event strings
  5. JSONRenderer — renders final dict to a JSON string

The stdlib root handler (StreamHandler → stdout) receives all log output.
The RedactionFilter is added to the root handler's handler-level filter chain,
so it applies to both structlog and any stdlib logging calls.
"""

from __future__ import annotations

__all__ = ["bind_tick_id", "configure_logging"]

import contextlib
import logging
import logging.handlers
import sys
from collections.abc import Generator
from pathlib import Path

import structlog

from src.observability.context import bot_id_var, tick_id_var

# Module-level flag so configure_logging() is idempotent.
_configured = False


def _add_context_vars(
    logger: logging.Logger,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Inject bot_id and tick_id from ContextVars into every log event.

    Args:
        logger: Unused — required by structlog processor signature.
        method_name: Unused — required by structlog processor signature.
        event_dict: The mutable event dictionary.

    Returns:
        Updated event_dict with bot_id and tick_id inserted when present.
    """
    bot_id = bot_id_var.get(None)
    tick_id = tick_id_var.get(None)
    if bot_id is not None:
        event_dict.setdefault("bot_id", bot_id)
    if tick_id is not None:
        event_dict.setdefault("tick_id", tick_id)
    return event_dict


def configure_logging(
    level: str = "info",
    log_dir: Path | None = None,
    process_name: str = "manager",
) -> None:
    """Configure structlog for JSON output with redaction.

    Idempotent — multiple calls are a no-op after the first.  The caller must
    have already called :func:`~src.secrets.install.configure_logging_with_redaction`
    (or :func:`~src.secrets.__init__.install_redaction`) so that the
    RedactionFilter is on the root logger before the first log line is emitted.

    Args:
        level: Log level string (e.g. "info", "debug", "warning").
        log_dir: If provided, also write JSON logs to
            ``<log_dir>/<process_name>.log`` in addition to stdout.
        process_name: Filename stem for the log file (e.g. "manager", "bot-id").
    """
    global _configured
    if _configured:
        return

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # ------------------------------------------------------------------
    # Root stdlib logger — output goes here; structlog will bridge into it.
    # ------------------------------------------------------------------
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove any handlers installed by earlier basicConfig calls.
    root.handlers.clear()

    # Stdout handler
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(numeric_level)
    root.addHandler(stdout_handler)

    # Optional file handler
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{process_name}.log"
        file_handler = logging.handlers.WatchedFileHandler(str(log_file), encoding="utf-8")
        file_handler.setLevel(numeric_level)
        root.addHandler(file_handler)

    # ------------------------------------------------------------------
    # structlog configuration
    # ------------------------------------------------------------------
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_context_vars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    _configured = True


@contextlib.contextmanager
def bind_tick_id(tick_id: str) -> Generator[None, None, None]:
    """Context manager that binds ``tick_id`` to structlog's bound contextvars.

    Also sets the contextvar directly so non-structlog stdlib logging calls
    and AuditLogger can read it via :func:`~src.observability.context.current_tick_id`.

    Args:
        tick_id: Correlation id for the current tick.

    Yields:
        None — used purely for its side effects on enter/exit.
    """
    token = tick_id_var.set(tick_id)
    structlog.contextvars.bind_contextvars(tick_id=tick_id)
    try:
        yield
    finally:
        tick_id_var.reset(token)
        structlog.contextvars.unbind_contextvars("tick_id")
