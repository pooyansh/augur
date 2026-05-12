"""Alert router — severity-based delivery to configured sinks.

Routing table (from CLAUDE.md § Alerting):

    info     → Discord
    warn     → Discord + Slack
    critical → Slack + Telegram

Every alert is:
1. Redacted (invariant 4 — no plaintext secret leaves the process).
2. Dedup-checked — suppressed if the same key was sent within 300 s.
3. Dispatched concurrently to all sinks for its severity level.
4. Sink failures are logged at ``warning`` and never raised to the caller.

Usage::

    router = make_default_router(secrets, redactor)
    await router.send(
        Severity.WARN,
        bot_id="bot-001",
        strategy="momentum_v1",
        market="BTC-100K",
        message="Signal stale for 5 minutes",
        template_key="signal_stale",
    )
"""

from __future__ import annotations

__all__ = [
    "AlertRouter",
    "Severity",
    "make_default_router",
]

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import blake2s

from src.alerts.dedup import Deduper
from src.alerts.sinks.base import Sink

logger = logging.getLogger(__name__)


class Severity(StrEnum):
    """Alert severity levels."""

    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


# Default routing: severity → list of sink names.
_DEFAULT_ROUTING: dict[str, list[str]] = {
    Severity.INFO: ["discord"],
    Severity.WARN: ["discord", "slack"],
    Severity.CRITICAL: ["slack", "telegram"],
}


class AlertRouter:
    """Routes alerts to the appropriate sinks based on severity.

    Args:
        sinks: Mapping of sink name to :class:`~src.alerts.sinks.base.Sink`
            instance (e.g. ``{"slack": SlackSink(...), ...}``).
        routing: Mapping of severity string to list of sink names.
        deduper: :class:`~src.alerts.dedup.Deduper` instance.
        redactor: Callable that redacts a message string.
    """

    def __init__(
        self,
        sinks: Mapping[str, Sink],
        routing: Mapping[str, list[str]],
        deduper: Deduper,
        redactor: Callable[[str], str],
    ) -> None:
        self._sinks = dict(sinks)
        self._routing = dict(routing)
        self._deduper = deduper
        self._redactor = redactor

    async def send(
        self,
        severity: Severity | str,
        *,
        bot_id: str,
        strategy: str,
        market: str,
        message: str,
        template_key: str,
        dedup_extra: str = "",
    ) -> None:
        """Send an alert to the sinks configured for ``severity``.

        Steps:
        1. Redact ``message`` (invariant 4).
        2. Build dedup key.
        3. Check dedup — skip if suppressed.
        4. Dispatch to configured sinks concurrently.
        5. Catch and log any sink failure (best-effort).

        Args:
            severity: Alert severity (:class:`Severity` or matching string).
            bot_id: Bot identifier included in the formatted message.
            strategy: Strategy name included in the formatted message.
            market: Market id included in the formatted message.
            message: Human-readable alert body.  MUST be the pre-redaction
                text; redaction is applied here before any sink sees it.
            template_key: Short stable string identifying the alert type
                (e.g. ``"signal_stale"``).  Used in the dedup key.
            dedup_extra: Optional additional string to differentiate otherwise
                identical template_keys (e.g. an error code).
        """
        severity_str = str(severity)

        # 1. Redact BEFORE any output path (invariant 4).
        safe_message = self._redactor(message)

        # 2. Dedup key: blake2s of the key components.
        raw_key = f"{bot_id}|{severity_str}|{template_key}|{dedup_extra}".encode()
        dedup_key = blake2s(raw_key, digest_size=8).hexdigest()

        # 3. Dedup check — use post-redaction message only from here on.
        now = datetime.now(tz=UTC)
        if self._deduper.seen(dedup_key, now):
            logger.debug(
                "Alert suppressed (dedup): key=%s severity=%s bot=%s template=%s",
                dedup_key,
                severity_str,
                bot_id,
                template_key,
            )
            return

        # 4. Assemble formatted body for each sink.
        formatted = (
            f"[{severity_str.upper()}] bot={bot_id} strategy={strategy} "
            f"market={market}\n{safe_message}"
        )

        # 5. Dispatch to configured sinks.
        target_names = self._routing.get(severity_str, [])
        tasks = []
        for name in target_names:
            sink = self._sinks.get(name)
            if sink is None:
                logger.debug("Sink %r not configured — skipping.", name)
                continue
            tasks.append(self._send_to_sink(sink, formatted))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=False)

    async def _send_to_sink(self, sink: Sink, message: str) -> None:
        """Deliver to a single sink, catching all errors (best-effort).

        Args:
            sink: The sink to deliver to.
            message: Already-redacted, formatted alert body.
        """
        try:
            await sink.send(message)
        except Exception as exc:
            logger.warning("Alert sink %r failed: %s", sink.name, exc)


def make_default_router(
    secrets_data: Mapping[str, object],
    redactor: Callable[[str], str],
) -> AlertRouter:
    """Build an :class:`AlertRouter` from the loaded secrets mapping.

    Reads the ``alerts`` top-level key from ``secrets_data``.  Each missing
    sink config logs a ``warning`` and that sink is omitted (never raises).

    Expected schema for ``secrets_data["alerts"]``::

        slack:    { webhook_url: "https://hooks.slack.com/..." }
        discord:  { webhook_url: "https://discord.com/api/webhooks/..." }
        telegram: { bot_token: "...", chat_id: "-100..." }

    Args:
        secrets_data: Top-level secrets mapping (keyed by file stem).
        redactor: Callable produced by :func:`~src.alerts.redact.make_redactor`.

    Returns:
        Configured :class:`AlertRouter` with whichever sinks are available.
    """
    from src.alerts.sinks.discord import DiscordSink
    from src.alerts.sinks.slack import SlackSink
    from src.alerts.sinks.telegram import TelegramSink

    alerts_cfg = secrets_data.get("alerts", {})
    if not isinstance(alerts_cfg, dict):
        logger.warning("alerts config is not a dict — all sinks disabled.")
        alerts_cfg = {}

    sinks: dict[str, Sink] = {}

    # Slack
    slack_cfg = alerts_cfg.get("slack", {})
    if isinstance(slack_cfg, dict) and slack_cfg.get("webhook_url"):
        sinks["slack"] = SlackSink(webhook_url=str(slack_cfg["webhook_url"]))
        logger.debug("Slack sink configured.")
    else:
        logger.warning("Slack sink: no webhook_url configured — sink disabled.")

    # Discord
    discord_cfg = alerts_cfg.get("discord", {})
    if isinstance(discord_cfg, dict) and discord_cfg.get("webhook_url"):
        sinks["discord"] = DiscordSink(webhook_url=str(discord_cfg["webhook_url"]))
        logger.debug("Discord sink configured.")
    else:
        logger.warning("Discord sink: no webhook_url configured — sink disabled.")

    # Telegram
    tg_cfg = alerts_cfg.get("telegram", {})
    if isinstance(tg_cfg, dict) and tg_cfg.get("bot_token") and tg_cfg.get("chat_id"):
        sinks["telegram"] = TelegramSink(
            bot_token=str(tg_cfg["bot_token"]),
            chat_id=str(tg_cfg["chat_id"]),
        )
        logger.debug("Telegram sink configured.")
    else:
        logger.warning("Telegram sink: missing bot_token or chat_id — sink disabled.")

    return AlertRouter(
        sinks=sinks,
        routing=_DEFAULT_ROUTING,
        deduper=Deduper(),
        redactor=redactor,
    )
