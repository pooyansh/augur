"""Discord webhook sink."""

from __future__ import annotations

__all__ = ["DiscordSink"]

import httpx

from src.alerts.sinks.base import Sink


class DiscordSink(Sink):
    """Delivers alerts to a Discord incoming-webhook URL.

    Args:
        webhook_url: Discord webhook URL.
        client: Optional ``httpx.AsyncClient`` for dependency injection in
            tests.  A new client is created per request when ``None``.
    """

    def __init__(
        self,
        webhook_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__("discord")
        self._webhook_url = webhook_url
        self._client = client

    async def send(self, message: str) -> None:
        """POST ``message`` as ``content`` to the Discord webhook.

        Args:
            message: Redacted alert text.

        Raises:
            httpx.HTTPError: On network or HTTP errors (caller catches).
        """
        payload = {"content": message}
        if self._client is not None:
            response = await self._client.post(self._webhook_url, json=payload)
            response.raise_for_status()
        else:
            async with httpx.AsyncClient() as client:
                response = await client.post(self._webhook_url, json=payload)
                response.raise_for_status()
