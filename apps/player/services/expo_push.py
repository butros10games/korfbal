"""Services for sending Expo push notifications to players."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import requests


logger = logging.getLogger(__name__)


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


def send_expo_push_tokens(*, tokens: list[str], payload: ExpoPushPayload) -> None:
    """Send Expo push notifications to the given tokens.

    Args:
        tokens: The Expo push tokens to send the notification to.
        payload: The payload of the notification.

    """
    if not tokens:
        return

    messages = [payload.to_message(token) for token in tokens if token]
    if not messages:
        return

    try:
        response = requests.post(
            "https://exp.host/--/api/v2/push/send",
            json=messages,
            timeout=10,
        )
        response.raise_for_status()
    except Exception:
        logger.warning("Failed sending Expo push tokens", exc_info=True)
