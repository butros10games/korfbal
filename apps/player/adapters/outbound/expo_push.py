"""Requests-backed Expo push adapter."""

from __future__ import annotations

from typing import Any

import requests

from apps.player.services.expo_push import ExpoPushClient


class RequestsExpoPushClient:
    """Production Expo push client backed by requests."""

    def send_messages(self, messages: list[dict[str, Any]]) -> None:
        """Send messages to Expo's push endpoint."""
        response = requests.post(
            "https://exp.host/--/api/v2/push/send",
            json=messages,
            timeout=10,
        )
        response.raise_for_status()


DEFAULT_EXPO_PUSH_CLIENT: ExpoPushClient = RequestsExpoPushClient()
