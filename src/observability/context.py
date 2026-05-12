"""Context variables for propagating tick_id and bot_id across async tasks.

These ContextVars are set at tick start in BaseBot.run and read by the
structlog processor to stamp every log line with the current tick/bot context.
AuditLogger.write also reads tick_id_var to stamp audit rows.
"""

from __future__ import annotations

__all__ = [
    "bot_id_var",
    "current_tick_id",
    "set_bot_id",
    "tick_id_var",
]

from contextvars import ContextVar

# Holds the current tick's correlation id.
# Set to a 16-char hex string at the start of each tick; cleared in finally.
tick_id_var: ContextVar[str | None] = ContextVar("tick_id_var", default=None)

# Holds the bot_id for the current process / async context.
# Set once at bot subprocess startup; stable for the lifetime of the process.
bot_id_var: ContextVar[str | None] = ContextVar("bot_id_var", default=None)


def current_tick_id() -> str | None:
    """Return the tick_id currently stored in the contextvar, or None.

    Returns:
        Current tick_id string, or None if not inside a tick.
    """
    return tick_id_var.get(None)


def set_bot_id(bot_id: str) -> None:
    """Set the bot_id contextvar for this async context.

    Should be called once at bot subprocess startup, before the run loop begins.

    Args:
        bot_id: Stable bot identifier.
    """
    bot_id_var.set(bot_id)
