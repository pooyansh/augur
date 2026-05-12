"""Invariant-4 guarantee: plaintext secrets must never reach a sink.

Tests use ``respx`` to intercept HTTP calls and assert the outbound body
is redacted.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from src.alerts.dedup import Deduper
from src.alerts.redact import make_redactor
from src.alerts.router import AlertRouter, Severity
from src.alerts.sinks.discord import DiscordSink
from src.alerts.sinks.slack import SlackSink
from src.alerts.sinks.telegram import TelegramSink

_SECRET = "supersecretvalue123"
_REDACTED = "***REDACTED***"

_DISCORD_URL = "https://discord.example.com/webhook"
_SLACK_URL = "https://slack.example.com/webhook"
_TELEGRAM_URL = "https://api.telegram.org/botFAKE_TOKEN/sendMessage"


def _make_router(client: httpx.AsyncClient) -> AlertRouter:
    """Build a router wired with a shared test httpx client."""
    redactor = make_redactor([_SECRET])
    sinks = {
        "discord": DiscordSink(webhook_url=_DISCORD_URL, client=client),
        "slack": SlackSink(webhook_url=_SLACK_URL, client=client),
        "telegram": TelegramSink(
            bot_token="FAKE_TOKEN",
            chat_id="-100test",
            client=client,
        ),
    }
    return AlertRouter(
        sinks=sinks,
        routing={
            Severity.INFO: ["discord"],
            Severity.WARN: ["discord", "slack"],
            Severity.CRITICAL: ["slack", "telegram"],
        },
        deduper=Deduper(),
        redactor=redactor,
    )


@pytest.mark.asyncio
async def test_discord_body_redacted() -> None:
    """Discord webhook body must not contain the plaintext secret."""
    captured: list[str] = []

    with respx.mock:
        respx.post(_DISCORD_URL).mock(side_effect=lambda req: _capture_and_respond(req, captured))

        async with httpx.AsyncClient() as client:
            router = _make_router(client)
            await router.send(
                Severity.INFO,
                bot_id="bot-001",
                strategy="test",
                market="market-x",
                message=f"Alert containing {_SECRET} in the body",
                template_key="test_event",
            )

    assert len(captured) == 1
    assert _SECRET not in captured[0], "Plaintext secret must not appear in Discord body"
    assert _REDACTED in captured[0], "Redacted placeholder must appear in Discord body"


@pytest.mark.asyncio
async def test_slack_body_redacted() -> None:
    """Slack webhook body must not contain the plaintext secret."""
    slack_captured: list[str] = []

    with respx.mock:
        # WARN goes to discord + slack; mock both so no warnings about unmocked calls
        respx.post(_DISCORD_URL).mock(return_value=httpx.Response(200))
        respx.post(_SLACK_URL).mock(
            side_effect=lambda req: _capture_and_respond(req, slack_captured)
        )

        async with httpx.AsyncClient() as client:
            router = _make_router(client)
            await router.send(
                Severity.WARN,
                bot_id="bot-001",
                strategy="test",
                market="market-x",
                message=f"Slack alert {_SECRET} embedded",
                template_key="test_slack",
                dedup_extra="slack_only",
            )

    assert len(slack_captured) == 1, "Slack must receive exactly one call"
    for body in slack_captured:
        assert _SECRET not in body, "Plaintext secret must not appear in Slack body"
        assert _REDACTED in body


@pytest.mark.asyncio
async def test_telegram_body_redacted() -> None:
    """Telegram sendMessage body must not contain the plaintext secret."""
    from urllib.parse import unquote_plus

    captured: list[str] = []

    with respx.mock:
        # CRITICAL goes to slack + telegram; mock both
        respx.post(_SLACK_URL).mock(return_value=httpx.Response(200))
        respx.post(_TELEGRAM_URL).mock(
            side_effect=lambda req: _capture_and_respond_form(req, captured)
        )

        async with httpx.AsyncClient() as client:
            router = _make_router(client)
            await router.send(
                Severity.CRITICAL,
                bot_id="bot-001",
                strategy="test",
                market="market-x",
                message=f"Critical: {_SECRET}",
                template_key="test_critical",
                dedup_extra="tg_only",
            )

    assert len(captured) >= 1
    for raw_body in captured:
        # Telegram uses form-encoded body; URL-decode before asserting.
        decoded = unquote_plus(raw_body)
        assert _SECRET not in decoded, "Plaintext secret must not appear in Telegram body"
        assert _REDACTED in decoded, "Redacted placeholder must appear in Telegram body"


@pytest.mark.asyncio
async def test_message_does_not_reach_sink_before_redaction() -> None:
    """Verifies the redaction happens before the HTTP body is built."""
    # Build a router with the secret in the redactor.
    raw_messages_sent: list[str] = []

    class SpySink(DiscordSink):
        async def send(self, message: str) -> None:
            raw_messages_sent.append(message)

    redactor = make_redactor([_SECRET])
    router = AlertRouter(
        sinks={"discord": SpySink(webhook_url=_DISCORD_URL)},
        routing={Severity.INFO: ["discord"]},
        deduper=Deduper(),
        redactor=redactor,
    )

    # We don't care that the HTTP call fails (no mock); only checking the value
    # passed to send().
    with respx.mock:
        respx.post(_DISCORD_URL).mock(return_value=httpx.Response(200))
        await router.send(
            Severity.INFO,
            bot_id="bot-001",
            strategy="test",
            market="market-x",
            message=f"Contains {_SECRET}",
            template_key="pre_redact_test",
        )

    assert len(raw_messages_sent) == 1
    msg = raw_messages_sent[0]
    assert _SECRET not in msg, "Sink.send must receive post-redaction text only"
    assert _REDACTED in msg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_and_respond(request: httpx.Request, captured: list[str]) -> httpx.Response:
    """Capture the JSON body text and return 200."""
    body = request.content.decode()
    captured.append(body)
    return httpx.Response(200)


def _capture_and_respond_form(request: httpx.Request, captured: list[str]) -> httpx.Response:
    """Capture form-encoded body text and return 200."""
    body = request.content.decode()
    captured.append(body)
    return httpx.Response(200)
