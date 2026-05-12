"""Telegram bot sink."""

from __future__ import annotations

__all__ = ["TelegramSink"]

import httpx

from src.alerts.sinks.base import Sink

_TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramSink(Sink):
    """Delivers alerts via the Telegram Bot API's ``sendMessage`` endpoint.

    Args:
        bot_token: Telegram bot token from BotFather.
        chat_id: Target chat or channel id.
        client: Optional ``httpx.AsyncClient`` for dependency injection in
            tests.  A new client is created per request when ``None``.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__("telegram")
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._client = client

    @property
    def _url(self) -> str:
        return f"{_TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"

    async def send(self, message: str) -> None:
        """POST ``message`` to the Telegram sendMessage endpoint.

        Uses form-encoded body as specified in the Telegram Bot API docs.

        Args:
            message: Redacted alert text.

        Raises:
            httpx.HTTPError: On network or HTTP errors (caller catches).
        """
        data = {"chat_id": self._chat_id, "text": message}
        if self._client is not None:
            response = await self._client.post(self._url, data=data)
            response.raise_for_status()
        else:
            async with httpx.AsyncClient() as client:
                response = await client.post(self._url, data=data)
                response.raise_for_status()
