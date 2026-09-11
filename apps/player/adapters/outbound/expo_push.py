"""Requests-backed Expo push adapter."""

from __future__ import annotations

from typing import Any

import requests


class RequestsExpoPushClient:
    """Production Expo push client backed by requests."""

    def send_messages(self, messages: list[dict[str, Any]]) -> None:
        """Send messages and reject per-ticket errors even on HTTP 200.

        Raises:
            RuntimeError: Expo rejected a notification ticket.

        """
        response = requests.post(
            "https://exp.host/--/api/v2/push/send",
            json=messages,
            timeout=10,
        )
        response.raise_for_status()
        tickets = response.json().get("data", [])
        if any(ticket.get("status") == "error" for ticket in tickets):
            raise RuntimeError("Expo rejected a notification")
