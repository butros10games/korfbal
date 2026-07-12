"""Tests for the Expo push application service."""

from __future__ import annotations

from typing import Any

from apps.player.services.expo_push import ExpoPushPayload, send_expo_push_tokens


class RecordingExpoClient:
    """Record outbound messages for assertions."""

    def __init__(self, *, should_fail: bool = False) -> None:
        """Initialize the recording client."""
        self.messages: list[dict[str, Any]] = []
        self.should_fail = should_fail

    def send_messages(self, messages: list[dict[str, Any]]) -> None:
        """Record messages or simulate a provider failure.

        Raises:
            RuntimeError: When provider failure simulation is enabled.

        """
        if self.should_fail:
            raise RuntimeError("provider unavailable")
        self.messages.extend(messages)


def test_send_expo_push_tokens_builds_messages_for_non_empty_tokens() -> None:
    """Only non-empty Expo tokens produce outbound messages."""
    client = RecordingExpoClient()
    payload = ExpoPushPayload(title="Goal", body="Scored", url="/matches/1")

    send_expo_push_tokens(
        tokens=["token-1", "", "token-2"],
        payload=payload,
        client=client,
    )

    assert client.messages == [
        {
            "to": "token-1",
            "title": "Goal",
            "body": "Scored",
            "data": {"url": "/matches/1"},
        },
        {
            "to": "token-2",
            "title": "Goal",
            "body": "Scored",
            "data": {"url": "/matches/1"},
        },
    ]


def test_send_expo_push_tokens_is_best_effort() -> None:
    """Provider failures do not escape the best-effort service boundary."""
    client = RecordingExpoClient(should_fail=True)

    send_expo_push_tokens(
        tokens=["token-1"],
        payload=ExpoPushPayload(title="Goal", body="Scored", url="/matches/1"),
        client=client,
    )
