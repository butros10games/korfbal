"""Services for sending Expo push notifications to players."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Protocol


logger = logging.getLogger(__name__)


class ExpoPushClient(Protocol):
    """Outbound Expo push provider port."""

    def send_messages(self, messages: list[dict[str, Any]]) -> None:
        """Send Expo push messages."""


@dataclass(frozen=True, slots=True)
class ExpoPushPayload:
    """Payload for an Expo push notification."""

    title: str
    body: str
    url: str

    def to_message(self, token: str) -> dict[str, Any]:
        """Convert the payload to a message dict for a specific token.

        Returns:
            dict[str, Any]: The message dict.

        """
        return {
            "to": token,
            "title": self.title,
            "body": self.body,
            "data": {"url": self.url},
        }


def send_expo_push_tokens(
    *,
    tokens: list[str],
    payload: ExpoPushPayload,
    client: ExpoPushClient,
) -> None:
    """Send Expo push notifications to the given tokens.

    Args:
        tokens: The Expo push tokens to send the notification to.
        payload: The payload of the notification.
        client: Outbound Expo provider implementation.

    """
    if not tokens:
        return

    messages = [payload.to_message(token) for token in tokens if token]
    if not messages:
        return

    try:
        client.send_messages(messages)
    except Exception:
        logger.warning("Failed sending Expo push tokens", exc_info=True)
