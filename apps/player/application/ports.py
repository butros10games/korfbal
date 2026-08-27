"""Outbound ports required by player application services."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import subprocess  # nosec B404
from typing import Any, BinaryIO, Protocol


@dataclass(frozen=True, slots=True)
class CommandRunOptions:
    """Options for a command-runner invocation."""

    check: bool
    capture_output: bool = False
    text: bool = False
    timeout: int | None = None


class CommandRunner(Protocol):
    """Run fixed argument-list commands."""

    def run(
        self,
        cmd: Sequence[str],
        options: CommandRunOptions,
    ) -> subprocess.CompletedProcess[str]:
        """Run the command and return its completed process."""


class AudioStorage(Protocol):
    """Store and retrieve generated player audio artifacts."""

    def exists(self, key: str) -> bool:
        """Return whether an artifact exists."""

    def save_bytes(self, key: str, content: bytes) -> str:
        """Persist bytes and return the stored key."""

    def url(self, key: str) -> str:
        """Return a client-facing URL for a stored key."""

    def open(self, key: str) -> BinaryIO:
        """Open a stored artifact for binary reading."""


@dataclass(frozen=True, slots=True)
class AudioRuntime:
    """Outbound capabilities needed to prepare player audio."""

    storage: AudioStorage
    commands: CommandRunner


class SongDownloadDispatcher(Protocol):
    """Dispatch asynchronous song-download work."""

    def cached_song(self, song_id: str) -> None:
        """Schedule download of a shared cached song."""

    def player_song(self, song_id: str) -> None:
        """Schedule download of a legacy player-owned song."""


class JobDispatchUnavailableError(RuntimeError):
    """Raised when background work cannot be handed to the job runtime."""


class WebPushDeliveryError(RuntimeError):
    """Provider-neutral web-push delivery failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Create a failure with the optional provider response status."""
        super().__init__(message)
        self.status_code = status_code


class WebPushClient(Protocol):
    """Deliver serialized web-push messages."""

    def send(
        self,
        *,
        subscription: dict[str, Any],
        data: str,
        ttl_seconds: int,
    ) -> None:
        """Send one message.

        Raises:
            WebPushDeliveryError: If the provider rejects delivery.

        """


class ExpoPushClient(Protocol):
    """Deliver Expo push messages."""

    def send_messages(self, messages: list[dict[str, Any]]) -> None:
        """Send a batch of Expo messages."""


class SpotifyResponse(Protocol):
    """Provider response consumed by Spotify use cases."""

    status_code: int
    text: str

    def json(self) -> dict[str, Any]:
        """Return the decoded response body."""

    def raise_for_status(self) -> None:
        """Raise for an unsuccessful response."""


class SpotifyClient(Protocol):
    """Exchange tokens, load profiles, and control Spotify playback."""

    def post_token(self, *, data: dict[str, Any]) -> SpotifyResponse:
        """Post to the token endpoint."""

    def get_current_user_profile(self, *, access_token: str) -> SpotifyResponse:
        """Load the current provider profile."""

    def put_playback(
        self,
        *,
        access_token: str,
        action: str,
        device_id: str | None,
        json_body: dict[str, Any] | None = None,
    ) -> SpotifyResponse:
        """Invoke a playback action."""
