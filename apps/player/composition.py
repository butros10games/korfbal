"""Composition helpers that wire player use cases to outbound adapters."""

from __future__ import annotations

from apps.player.adapters.outbound.expo_push import DEFAULT_EXPO_PUSH_CLIENT
from apps.player.services.expo_push import ExpoPushPayload, send_expo_push_tokens


def send_expo_push(*, tokens: list[str], payload: ExpoPushPayload) -> None:
    """Send Expo push messages with the production outbound adapter."""
    send_expo_push_tokens(
        tokens=tokens,
        payload=payload,
        client=DEFAULT_EXPO_PUSH_CLIENT,
    )
